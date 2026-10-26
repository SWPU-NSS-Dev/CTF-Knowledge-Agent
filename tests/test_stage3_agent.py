from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from ctfka.agent import AgentService
from ctfka.config import AgentConfig
from ctfka.contracts import FetchedDocument, ParsedDocument
from ctfka.knowledge.fixtures import seed_awdp_fixture
from ctfka.models import Store

BODY = "# SQL challenge\n\nThe vulnerable query uses string concatenation.\n\nUse a parameterized query."


class MemoryStore:
    def search(self, query, limit=20, source=None):
        return [
            {
                "id": "article-1",
                "version": 2,
                "url": "https://example.test/writeup",
                "title": "SQL challenge",
                "content": BODY,
                "evidence_offsets": [{"start": 22, "end": 48}],
                "source": "fixture",
            }
        ]

    def list_entities(self, **kwargs):
        return [
            {
                "id": "v-1",
                "kind": "vulnerability",
                "title": "SQL injection",
                "verification_status": "unverified",
            }
        ]

    def get_article_version(self, article_id, version):
        if article_id != "article-1" or version not in (1, 2):
            return None
        old = "# SQL challenge\n\nThe original source says use concatenation."
        return {
            "id": article_id,
            "version": version,
            "url": "https://example.test/writeup",
            "title": "SQL challenge",
            "content": BODY if version == 2 else old,
        }

    def related(self, entity_id, **kwargs):
        return {"entities": [], "relations": [], "warnings": []}

    def find_attack_fix(self, query, **kwargs):
        return {
            "entities": [],
            "relations": [],
            "comparisons": [
                {"attack_id": "a", "fix_id": "f1", "status": "candidate"},
                {"attack_id": "a", "fix_id": "f2", "status": "human_verified"},
            ],
            "warnings": [],
        }


class GoodProvider:
    async def chat(self, messages, *, model=None, **kwargs):
        assert "untrusted data" in messages[0]["content"]
        return {"message": {"content": "Use parameterized queries [S1]."}, "eval_count": 11}


def settings(**agent):
    return SimpleNamespace(
        agent=AgentConfig(**agent),
        llm=SimpleNamespace(chat_model="local-test", options={}, max_context_tokens=8192),
    )


@pytest.mark.asyncio
async def test_agent_multistep_trace_citations_and_candidate_comparison():
    service = AgentService(settings(max_tool_calls=8), MemoryStore(), GoodProvider())
    result = await service.answer("Compare SQL injection fixes", entity_id="v-1")
    assert result["inference_status"] == "ai_inferred"
    assert result["grounded"] is True
    assert result["citations"][0]["version"] == 2
    assert result["citations"][0]["quote"] == BODY[22:48]
    assert len(result["trace"]) >= 4
    assert {t["name"] for t in result["trace"]} >= {
        "search_knowledge",
        "search_writeups",
        "read_article",
        "find_related",
        "find_attack_fix",
    }
    assert {x["status"] for x in result["comparisons"]} == {"candidate", "human_verified"}


@pytest.mark.asyncio
async def test_agent_no_model_and_unknown_tool_citation_are_safe():
    service = AgentService(settings(max_tool_calls=None), MemoryStore(), None)
    result = await service.answer("SQL challenge")
    assert result["inference_status"] == "deterministic_evidence"
    assert result["grounded"] is True
    with pytest.raises(ValueError):
        await service.tool("run_shell", {"command": "echo nope"})
    with pytest.raises(ValueError):
        await service.tool("read_article", {"path": "C:/secret"})


@pytest.mark.asyncio
async def test_agent_rejects_unlisted_model_citations():
    class BadProvider:
        async def chat(self, *args, **kwargs):
            return {"message": {"content": "Trust me [S9]."}}

    result = await AgentService(settings(), MemoryStore(), BadProvider()).answer("SQL challenge")
    assert result["grounded"] is False
    assert "withheld" in result["warnings"][-1]


@pytest.mark.asyncio
async def test_agent_does_not_accept_uncited_model_text_as_ai_inference():
    class NoCitationProvider:
        async def chat(self, *args, **kwargs):
            return {"message": {"content": "Always use a parameterized query."}}

    result = await AgentService(settings(), MemoryStore(), NoCitationProvider()).answer(
        "SQL challenge"
    )
    assert result["inference_status"] == "deterministic_evidence"
    assert result["answer"].startswith("检索到以下已存证据")
    assert result["citations"]
    assert any("no source citations" in warning for warning in result["warnings"])


@pytest.mark.asyncio
async def test_agent_respects_tool_budget_and_detects_no_repeated_calls():
    result = await AgentService(settings(max_tool_calls=2), MemoryStore(), None).answer(
        "compare SQL fixes", entity_id="v-1"
    )
    assert len(result["trace"]) == 2
    keys = [(item["name"], str(sorted(item["args"].items()))) for item in result["trace"]]
    assert len(keys) == len(set(keys))
    assert any("调用上限" in warning for warning in result["warnings"])


@pytest.mark.asyncio
async def test_agent_timeout_and_cancellation_do_not_close_shared_provider():
    class SlowProvider:
        def __init__(self):
            self.cancelled = False
            self.closed = False
            self.started = asyncio.Event()

        async def chat(self, *args, **kwargs):
            self.started.set()
            try:
                await asyncio.sleep(5)
            except asyncio.CancelledError:
                self.cancelled = True
                raise

        async def close(self):
            self.closed = True

    timed = await AgentService(
        settings(timeout_seconds=0.01), MemoryStore(), SlowProvider()
    ).answer("SQL challenge")
    assert any("超时" in warning for warning in timed["warnings"])
    provider = SlowProvider()
    task = asyncio.create_task(
        AgentService(settings(timeout_seconds=None), MemoryStore(), provider).answer(
            "SQL challenge"
        )
    )
    await asyncio.wait_for(provider.started.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert provider.cancelled is True
    assert provider.closed is False


@pytest.mark.asyncio
async def test_agent_timeout_covers_read_only_planning_calls():
    class SlowStore(MemoryStore):
        def list_entities(self, **kwargs):
            import time

            time.sleep(0.2)
            return []

    result = await AgentService(settings(timeout_seconds=0.01), SlowStore(), None).answer(
        "SQL challenge"
    )
    assert result["grounded"] is False
    assert "超时" in result["answer"]
    assert "超时" in result["warnings"][0]


@pytest.mark.asyncio
async def test_agent_with_real_store_quotes_graph_and_historical_versions(tmp_path):
    store = Store(tmp_path)
    seeded = seed_awdp_fixture(store)
    original = store.get_article_version(seeded["article_id"], 1)
    assert original and original["content"]

    # Advance the current source while the graph keeps immutable evidence at version 1.
    changed = original["content"] + "\n\nLater source revision with new context."
    store.ingest(
        "fixture-awdp",
        FetchedDocument(
            url=original["url"],
            raw=changed.encode(),
            metadata={"identity": original["identity"], "fixture": True},
        ),
        ParsedDocument(title=original["title"], content=changed),
    )
    assert store.get_article_version(seeded["article_id"], 1)["content"] == original["content"]

    result = await AgentService(settings(max_tool_calls=None), store, None).answer(
        "AWDP题目中的路径遍历漏洞，如何比较修复方案？"
    )
    assert any(
        row["name"] == "compare_solutions" and row["status"] == "ok" for row in result["trace"]
    )
    assert len(result["comparisons"]) >= 2
    assert all(row["status"] == "candidate" for row in result["comparisons"])
    assert result["citations"]
    assert any(citation["version"] == 1 for citation in result["citations"])
    for citation in result["citations"]:
        source = store.get_article_version(citation["article_id"], citation["version"])
        assert source is not None
        body = source["content"]
        assert citation["quote"] == body[citation["start"] : citation["end"]]
    historical = await AgentService(settings(), store, None).tool(
        "read_article", {"article_id": seeded["article_id"], "version": 1}
    )
    assert historical["article"]["content"] == original["content"]


@pytest.mark.asyncio
async def test_candidate_prefix_and_comparison_context_reach_model(tmp_path):
    store = Store(tmp_path)
    seed_awdp_fixture(store)

    class CaptureProvider:
        messages = None

        async def chat(self, messages, *, model=None, **kwargs):
            self.messages = messages
            return {"message": {"content": "Fix A enforces the workspace boundary [S1]."}}

    provider = CaptureProvider()
    result = await AgentService(settings(), store, provider).answer(
        "AWDP题目中的路径遍历漏洞，如何比较修复方案？"
    )
    prefix = "以下比较包含候选关系，尚未人工核实。"
    assert result["answer"].startswith(prefix + "\n\n")
    prompt = json.loads(provider.messages[-1]["content"])
    comparisons = prompt["comparisons"]
    assert len(comparisons) == 2
    assert all(item["status"] == "candidate" for item in comparisons)
    assert {item["fix_title"] for item in comparisons} == {
        "Resolve and enforce workspace boundary",
        "Disable redirects and absolute paths",
    }
    assert {item["tradeoff"] for item in comparisons} == {
        "symlink compatibility",
        "remote preview unavailable",
    }
    assert {patch for item in comparisons for patch in item["patch_titles"]} == {
        "Post-resolution containment check",
        "Reject redirects and absolute paths",
    }
    prompt_labels = {item["label"] for item in prompt["evidence"]}
    assert all(set(item["evidence_labels"]).issubset(prompt_labels) for item in comparisons)
    assert (
        "A historical attack description does not show that a fix"
        in provider.messages[0]["content"]
    )


@pytest.mark.asyncio
async def test_agent_context_budget_omits_whole_utf8_evidence_blocks():
    body = "SQL evidence 漏洞 " + ("complete-source-block " * 25)

    class ManyArticles(MemoryStore):
        def search(self, query, limit=20, source=None):
            return [
                {
                    "id": f"article-{index}",
                    "version": 4,
                    "url": f"https://example.test/{index}",
                    "title": f"Evidence {index}",
                    "content": body,
                    "evidence_offsets": [{"start": 0, "end": len(body)}],
                }
                for index in range(5)
            ]

        def get_article_version(self, article_id, version):
            return {
                "id": article_id,
                "version": version,
                "url": "https://example.test/history",
                "title": "History",
                "content": body,
            }

    class CapturingProvider:
        messages = None

        async def chat(self, messages, *, model=None, **kwargs):
            self.messages = messages
            return {"message": {"content": "Grounded [S1]."}}

    provider = CapturingProvider()
    result = await AgentService(
        settings(max_context_tokens=2600, max_retrieval_results=10, max_tool_calls=None),
        ManyArticles(),
        provider,
    ).answer("SQL evidence")
    assert provider.messages is not None, result["warnings"]
    prompt_data = json.loads(provider.messages[-1]["content"])
    assert prompt_data["evidence"]
    assert any("省略了一整段证据原文" in warning for warning in result["warnings"])
    assert result["citations"][0]["quote"] == body
    assert prompt_data["evidence"][0]["quote"] == result["citations"][0]["quote"]
