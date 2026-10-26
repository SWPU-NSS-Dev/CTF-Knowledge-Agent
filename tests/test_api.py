import asyncio
import time

from fastapi.testclient import TestClient

from ctfka.api.app import create_app
from ctfka.config import Settings, SourceConfig
from ctfka.contracts import DiscoveredItem, FetchedDocument
from ctfka.models.store import Store


def test_search_and_admin_auth(tmp_path):
    settings = Settings(data_dir=tmp_path, token="secret")
    store = Store(tmp_path)
    store.register_source("fixture", "html", {})
    store.ingest("fixture", FetchedDocument("https://example.test/a", b"# CVE reference\nCVE-2024-12345"))
    client = TestClient(create_app(settings))

    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/api/v1/search", params={"q": "CVE-2024-12345"}).status_code == 200
    assert client.post("/api/v1/index/rebuild").status_code == 401
    response = client.post("/api/v1/index/rebuild", headers={"Authorization": "Bearer secret"})
    assert response.status_code == 200


def test_source_api_rejects_unregistered_adapter(tmp_path):
    client = TestClient(create_app(Settings(data_dir=tmp_path, token="secret")))
    response = client.put(
        "/api/v1/sources/arbitrary",
        headers={"Authorization": "Bearer secret"},
        json={"adapter": "os:system"},
    )
    assert response.status_code == 422


def test_installed_source_config_persists(tmp_path):
    settings = Settings(data_dir=tmp_path, token="secret")
    with TestClient(create_app(settings)) as client:
        response = client.put(
            "/api/v1/sources/custom-html",
            headers={"Authorization": "Bearer secret"},
            json={"adapter": "html", "options": {"start_urls": ["https://example.org/"]}},
        )
        assert response.status_code == 200
    reopened = TestClient(create_app(Settings(data_dir=tmp_path, token="secret")))
    configs = {source["name"]: source for source in reopened.get("/api/v1/sources").json()["items"]}
    assert configs["custom-html"]["adapter"] == "html"
    assert configs["custom-html"]["options"]["start_urls"] == ["https://example.org/"]


def test_admin_is_disabled_without_token(tmp_path):
    client = TestClient(create_app(Settings(data_dir=tmp_path)))
    response = client.post("/api/v1/index/rebuild")
    assert response.status_code == 503


def test_chat_admin_auth_fails_closed_and_unsupported_parameters_are_422(tmp_path):
    disabled = TestClient(create_app(Settings(data_dir=tmp_path / "disabled")))
    assert disabled.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "hi"}]}).status_code == 503

    enabled = TestClient(create_app(Settings(data_dir=tmp_path / "enabled", token="secret")))
    response = enabled.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer secret"},
        json={"messages": [{"role": "user", "content": "hi"}], "tools": []},
    )
    assert response.status_code == 422
    assert "tools" in response.text

    response = enabled.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer secret"},
        json={"messages": [{"role": "user", "content": "hi"}], "n": 2},
    )
    assert response.status_code == 422
    assert "n greater than 1" in response.text


def test_chat_alias_is_registered(tmp_path):
    app = create_app(Settings(data_dir=tmp_path, token="secret"))
    paths = {route.path for route in app.routes}
    assert "/v1/chat/completions" in paths
    assert "/api/v1/chat/completions" in paths


def test_openai_chat_shapes_and_sse_terminal(tmp_path, monkeypatch):
    from ctfka.llm import ollama
    from ctfka.retrieval import rag

    class FakeProvider:
        def __init__(self, config):
            pass

        async def status(self):
            return {"reachable": True}

        async def list_models(self):
            return [{"name": "local-model:latest", "digest": "digest"}]

        async def close(self):
            pass

    result = {
        "answer": "Hi",
        "model": "local-model",
        "citations": [],
        "usage": {"prompt_tokens": None, "completion_tokens": None, "total_tokens": None},
        "retrieval_mode": "keyword",
        "warnings": [],
        "grounded": False,
        "inference_status": "no_evidence",
        "finish_reason": "stop",
    }

    class FakeRag:
        def __init__(self, settings, store, provider):
            pass

        async def answer(self, messages, *, model=None, options=None):
            if messages[-1]["content"] == "Truncate":
                return {**result, "answer": "partial", "finish_reason": "length"}
            return result

        async def stream(self, messages, *, model=None, options=None):
            if messages[-1]["content"] == "Hi":
                yield {"type": "delta", "content": "Hi"}
                yield {"type": "result", "result": result}
            elif messages[-1]["content"] == "Truncate":
                yield {"type": "result", "result": {**result, "answer": "partial", "finish_reason": "length"}}
            else:
                yield {"type": "result", "result": {**result, "answer": "No evidence answer", "inference_status": "no_evidence"}}

    monkeypatch.setattr(ollama, "OllamaProvider", FakeProvider)
    monkeypatch.setattr(rag, "RAGService", FakeRag)
    headers = {"Authorization": "Bearer secret"}
    with TestClient(create_app(Settings(data_dir=tmp_path, token="secret"))) as client:
        response = client.post("/v1/chat/completions", headers=headers, json={"model": "local-model", "messages": [{"role": "user", "content": "Hi"}]})
        assert response.status_code == 200
        payload = response.json()
        assert payload["object"] == "chat.completion"
        assert payload["choices"][0]["message"] == {"role": "assistant", "content": "Hi"}
        assert payload["choices"][0]["finish_reason"] == "stop"
        stream = client.post("/api/v1/chat/completions", headers=headers, json={"model": "local-model", "stream": True, "messages": [{"role": "user", "content": "Hi"}]})
        assert stream.status_code == 200
        assert '"role":"assistant"' in stream.text
        assert '"content":"Hi"' in stream.text
        assert '"finish_reason":"stop"' in stream.text
        assert "data: [DONE]\n\n" in stream.text
        truncated = client.post("/v1/chat/completions", headers=headers, json={"model": "local-model", "max_tokens": 5, "messages": [{"role": "user", "content": "Truncate"}]})
        assert truncated.json()["choices"][0]["finish_reason"] == "length"
        truncated_stream = client.post("/v1/chat/completions", headers=headers, json={"model": "local-model", "max_tokens": 5, "stream": True, "messages": [{"role": "user", "content": "Truncate"}]})
        assert '"finish_reason":"length"' in truncated_stream.text
        degraded = client.post("/v1/chat/completions", headers=headers, json={"model": "local-model", "stream": True, "messages": [{"role": "user", "content": "No evidence"}]})
        assert '"content":"No evidence answer"' in degraded.text
        assert '"inference_status":"no_evidence"' in degraded.text


async def test_chat_stream_cancellation_closes_pending_rag_iterator(tmp_path):
    from fastapi import Request

    from ctfka.api.app import ChatRequest

    started = asyncio.Event()
    closed = asyncio.Event()

    class Provider:
        async def status(self):
            return {"reachable": True}

        async def list_models(self):
            return [{"name": "local-model:latest"}]

    class Rag:
        async def stream(self, messages, *, model=None, options=None):
            try:
                started.set()
                await asyncio.Future()
                yield {"type": "delta", "content": "unreachable"}
            finally:
                closed.set()

    application = create_app(Settings(data_dir=tmp_path, token="secret"))
    application.state.provider = Provider()
    application.state.rag_service = Rag()
    route = next(route for route in application.routes if route.path == "/v1/chat/completions")

    async def receive():
        await asyncio.sleep(3600)
        return {"type": "http.disconnect"}

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/v1/chat/completions",
        "raw_path": b"/v1/chat/completions",
        "query_string": b"",
        "headers": [],
        "client": ("testclient", 123),
        "server": ("testserver", 80),
    }
    request = Request(scope, receive=receive)
    response = await route.endpoint(
        request,
        ChatRequest(model="local-model", stream=True, messages=[{"role": "user", "content": "wait"}]),
    )
    body_iterator = response.body_iterator
    first = await body_iterator.__anext__()
    assert '"role":"assistant"' in first
    pending = asyncio.create_task(body_iterator.__anext__())
    await asyncio.wait_for(started.wait(), timeout=1)
    pending.cancel()
    try:
        await pending
    except asyncio.CancelledError:
        pass
    await asyncio.wait_for(closed.wait(), timeout=1)


def test_background_crawl_failure_is_visible_and_duplicate_is_rejected(tmp_path, monkeypatch):
    from ctfka.scheduler.worker import CrawlService

    async def controlled_failure(self, source, max_jobs=None):
        await asyncio.sleep(0.5)
        raise RuntimeError("controlled background failure")

    monkeypatch.setattr(CrawlService, "crawl", controlled_failure)
    settings = Settings(
        data_dir=tmp_path,
        token="secret",
        sources={"broken": SourceConfig(adapter="not-installed")},
    )
    with TestClient(create_app(settings)) as client:
        headers = {"Authorization": "Bearer secret"}
        first = client.post("/api/v1/crawl/jobs", headers=headers, json={"source": "broken"})
        assert first.status_code == 202
        duplicate = client.post("/api/v1/crawl/jobs", headers=headers, json={"source": "broken"})
        assert duplicate.status_code == 409
        run_id = first.json()["run_id"]
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            runs = client.get("/api/v1/crawl/status").json()["runs"]
            run = next((item for item in runs if item["id"] == run_id), None)
            if run and run["status"] == "failed":
                break
            time.sleep(0.02)
        assert run["status"] == "failed"
        assert "controlled background failure" in run["error"]


def test_api_run_with_failed_jobs_is_not_reported_as_completed(tmp_path, monkeypatch):
    from ctfka.scheduler.worker import CrawlService

    async def partial_failure(self, source, max_jobs=None):
        return {
            "source": source,
            "run": {
                "claimed": 1,
                "completed": 0,
                "failed": 1,
                "errors": [{"job_id": "job-1", "error": "blocked URL"}],
            },
        }

    monkeypatch.setattr(CrawlService, "crawl", partial_failure)
    settings = Settings(
        data_dir=tmp_path,
        token="secret",
        sources={"fixture": SourceConfig(adapter="ctfwiki")},
    )
    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/api/v1/crawl/jobs",
            headers={"Authorization": "Bearer secret"},
            json={"source": "fixture"},
        )
        assert response.status_code == 202
        run_id = response.json()["run_id"]
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            run = next(
                item for item in client.get("/api/v1/crawl/status").json()["runs"]
                if item["id"] == run_id
            )
            if run["status"] != "running":
                break
            time.sleep(0.02)
        assert run["status"] == "completed_with_errors"
        assert run["errors"] == [{"job_id": "job-1", "error": "blocked URL"}]


def test_lifespan_worker_resumes_persisted_jobs_after_restart(tmp_path):
    settings = Settings(
        data_dir=tmp_path,
        sources={"broken": SourceConfig(adapter="not-installed", max_attempts=1)},
    )
    store = Store(tmp_path)
    store.register_source("broken", "not-installed", settings.sources["broken"].model_dump())

    for identity in ("before-first-start", "before-second-start"):
        store.enqueue_job("broken", DiscoveredItem(f"https://example.test/{identity}", identity))
        with TestClient(create_app(settings)) as client:
            assert client.get("/api/v1/crawl/status").json()["worker"]["status"] == "running"
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                jobs = store.list_jobs(limit=100)
                item = next(job for job in jobs if job["identity"] == identity)
                if item["status"] == "failed":
                    break
                time.sleep(0.02)
            assert item["status"] == "failed"
