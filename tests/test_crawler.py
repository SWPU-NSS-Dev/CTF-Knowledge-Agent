from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from ctfka.config import Settings, SourceConfig
from ctfka.contracts import DiscoveredItem, FetchedDocument, ParsedDocument
from ctfka.crawler.builtin import GitHubAdapter
from ctfka.crawler.http import FetchClient, FetchError
from ctfka.models import Store
from ctfka.scheduler.worker import CrawlService
from ctfka.security import PublicURL, URLBlocked, validate_public_url

FIXTURES = Path(__file__).parent / "fixtures"


class FixtureResponse:
    def __init__(self, value, *, status_code=200, headers=None):
        self._value = value
        self.status_code = status_code
        self.headers = headers or {}

    def json(self):
        return self._value


class FixtureGitHubClient:
    async def get(self, url, headers=None):
        if "/commits/" in url:
            value = json.loads((FIXTURES / "crawler_github_commit.json").read_text())
        elif "/git/trees/root-tree-sha" in url:
            value = json.loads((FIXTURES / "crawler_github_root_tree.json").read_text())
        elif "/git/trees/writeups-tree" in url:
            value = json.loads((FIXTURES / "crawler_github_writeups_tree.json").read_text())
        elif "/git/trees/year-tree" in url:
            value = json.loads((FIXTURES / "crawler_github_year_tree.json").read_text())
        elif "/git/trees/docs-tree" in url:
            value = json.loads((FIXTURES / "crawler_github_docs_tree.json").read_text())
        else:
            raise AssertionError(f"unexpected GitHub request: {url}")
        return FixtureResponse(value)


@pytest.mark.asyncio
async def test_github_tree_walk_preserves_nested_paths_and_same_named_files():
    config = SourceConfig(
        adapter="github", options={"owner": "public", "repo": "writeups", "ref": "main"}
    )
    adapter = GitHubAdapter(config, FixtureGitHubClient())
    found = [item async for item in adapter.discover()]

    assert {item.metadata["path"] for item in found} == {
        "docs/index.html",
        "writeups/README.md",
        "writeups/2025/final.md",
        "README.md",
    }
    assert len({item.identity for item in found}) == 4
    assert adapter.complete_scan
    assert len(adapter.current_identities) == 4


@pytest.mark.asyncio
async def test_github_incremental_scan_keeps_unchanged_files_in_archive_set():
    config = SourceConfig(
        adapter="github", options={"owner": "public", "repo": "writeups", "ref": "main"}
    )
    adapter = GitHubAdapter(config, FixtureGitHubClient())
    previous = {
        "commit": "old-commit",
        "files": {
            "README.md": "root-readme-blob",
            "writeups/README.md": "writeups-readme-blob",
            "writeups/2025/final.md": "old-final-blob",
            "docs/index.html": "docs-index-blob",
        },
    }
    changed = [item async for item in adapter.discover(previous)]

    assert [item.metadata["path"] for item in changed] == ["writeups/2025/final.md"]
    assert len(adapter.current_identities) == 4
    assert adapter.complete_scan


class ConditionalGitHubClient(FixtureGitHubClient):
    def __init__(self, responses):
        self.responses = list(responses)
        self.commit_headers = []

    async def get(self, url, headers=None):
        if "/commits/" in url:
            self.commit_headers.append(dict(headers or {}))
            response = self.responses.pop(0)
            if isinstance(response, FixtureResponse):
                if response.status_code == 200 and not response._value:
                    return FixtureResponse(
                        json.loads((FIXTURES / "crawler_github_commit.json").read_text()),
                        headers={"ETag": '"new-etag"'},
                    )
                return response
            return FixtureResponse(
                json.loads((FIXTURES / "crawler_github_commit.json").read_text()),
                headers={"ETag": '"new-etag"'},
            )
        return await super().get(url, headers)


@pytest.mark.asyncio
async def test_github_304_uses_only_committed_local_tree():
    client = ConditionalGitHubClient([FixtureResponse({}, status_code=304)])
    adapter = GitHubAdapter(
        SourceConfig(
            adapter="github", options={"owner": "public", "repo": "writeups", "ref": "main"}
        ),
        client,
    )
    previous = {
        "commit": "old-commit",
        "commit_etag": '"old-etag"',
        "files": {"README.md": "root-readme-blob"},
    }

    found = [item async for item in adapter.discover(previous)]

    assert found == []
    assert client.commit_headers[0]["If-None-Match"] == '"old-etag"'
    assert adapter.state == previous
    assert not adapter.complete_scan


@pytest.mark.asyncio
async def test_github_cold_304_retries_unconditionally_and_captures_etag():
    client = ConditionalGitHubClient(
        [FixtureResponse({}, status_code=304), FixtureResponse({}, status_code=200)]
    )
    adapter = GitHubAdapter(
        SourceConfig(
            adapter="github", options={"owner": "public", "repo": "writeups", "ref": "main"}
        ),
        client,
    )

    found = [item async for item in adapter.discover()]

    assert len(found) == 4
    assert "If-None-Match" not in client.commit_headers[0]
    assert "If-None-Match" not in client.commit_headers[1]
    assert adapter.complete_scan
    assert adapter.state["commit_etag"] == '"new-etag"'


class StaticAdapter:
    def __init__(self, config, client):
        self.config = config
        self.client = client
        self._state = {}

    @property
    def state(self):
        return self._state

    def set_state(self, state):
        self._state = dict(state or {})

    async def discover(self, cursor=None):
        yield DiscoveredItem("https://example.org/writeup", "fixture:writeup")

    async def fetch(self, item):
        return FetchedDocument(
            item.url, b"# Fixture Writeup\n\nflag{persistent_job}", "text/markdown"
        )

    def parse(self, document):
        return ParsedDocument("Fixture Writeup", document.raw.decode(), {})

    def normalize(self, parsed):
        return parsed

    def validate(self, parsed):
        assert parsed.content


class DummyClient:
    def __init__(self, config):
        self.config = config

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None


class GitHubLikeAdapter(StaticAdapter):
    def __init__(self, config, client):
        super().__init__(config, client)
        self.complete_scan = True
        self.current_identities = ["fixture:a.md"]
        self.fail_once = True

    async def discover(self, cursor=None):
        self._state = {"commit": "new-commit", "files": {"a.md": "blob-a"}}
        yield DiscoveredItem("https://example.org/a.md", "fixture:a.md")

    async def fetch(self, item):
        if self.fail_once:
            self.fail_once = False
            raise RuntimeError("temporary fetch failure")
        return FetchedDocument(item.url, b"# A\n\nbody", "text/markdown")


@pytest.mark.asyncio
async def test_worker_persists_discovered_item_and_completes_durable_job(tmp_path, monkeypatch):
    monkeypatch.setattr("ctfka.scheduler.worker.FetchClient", DummyClient)
    settings = Settings(
        data_dir=tmp_path,
        workers=2,
        sources={"fixtures": SourceConfig(adapter="ctfwiki", options={})},
    )
    store = Store(tmp_path)
    service = CrawlService(settings, store)
    service.registry.create = lambda name, config, client: StaticAdapter(config, client)
    try:
        result = await service.crawl("fixtures")
        assert result["discovery"]["queued"] == 1
        assert result["run"]["completed"] == 1
        assert store.job_stats().get("completed") == 1
        article = store.search("persistent_job", limit=1)[0]
        assert article["url"] == "https://example.org/writeup"
        assert article["identity"] == "fixture:writeup"
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_github_cursor_waits_for_all_changed_file_jobs(tmp_path, monkeypatch):
    monkeypatch.setattr("ctfka.scheduler.worker.FetchClient", DummyClient)
    settings = Settings(
        data_dir=tmp_path,
        sources={"repo": SourceConfig(adapter="github", options={})},
    )
    store = Store(tmp_path)
    service = CrawlService(settings, store)
    service.registry.create = lambda name, config, client: GitHubLikeAdapter(config, client)
    try:
        discovery = await service.discover("repo")
        assert discovery["pending"]
        state = store.get_source_state("repo")
        assert "commit" not in state
        job_id = state["pending"]["job_ids"][0]

        result = await service.run("repo")
        assert result["failed"] == 1
        assert "commit" not in store.get_source_state("repo")

        assert store.retry_job(job_id)
        result = await service.run("repo")
        assert result["completed"] == 1
        assert store.get_source_state("repo")["commit"] == "new-commit"
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_public_url_boundary_rejects_local_addresses():
    with pytest.raises(URLBlocked):
        await validate_public_url("http://127.0.0.1/admin", resolve=False)


@pytest.mark.asyncio
async def test_redirect_destination_is_validated_before_connecting(monkeypatch):
    checked = []

    async def validate(url, *, resolve=True):
        checked.append(url)
        if "127.0.0.1" in url:
            raise URLBlocked("non-public IP address")
        return PublicURL(url, "public.example", 443, ("93.184.216.34",))

    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        return httpx.Response(302, headers={"Location": "http://127.0.0.1/private"})

    monkeypatch.setattr("ctfka.crawler.http.validate_public_url", validate)
    client = FetchClient(SourceConfig(adapter="html"))
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False)
    try:
        with pytest.raises(FetchError, match="non-public") as caught:
            await client.get("https://public.example/start")
        assert caught.value.category == "blocked"
        assert "http://127.0.0.1/private" in checked
    finally:
        await client._client.aclose()


@pytest.mark.asyncio
async def test_fetch_client_bounds_response_bytes_and_preserves_retry_after(monkeypatch):
    observed = []

    def handler(request):
        observed.append(request)
        if request.url.path == "/large":
            return httpx.Response(200, content=b"123456")
        if request.url.path == "/limited":
            return httpx.Response(429, headers={"Retry-After": "13"})
        raise AssertionError(f"unexpected request: {request.url}")

    async def validate(url, *, resolve=True):
        return PublicURL(url, "public.example", 443, ("93.184.216.34",))

    client = FetchClient(
        SourceConfig(adapter="html", options={"respect_robots": False}, max_response_bytes=5)
    )
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False)
    try:
        monkeypatch.setattr("ctfka.crawler.http.validate_public_url", validate)
        with pytest.raises(FetchError, match="max_response_bytes") as large:
            await client.get("https://public.example/large")
        assert large.value.category == "blocked"
        assert observed[0].url.host == "93.184.216.34"
        assert observed[0].headers["Host"] == "public.example"
        assert observed[0].extensions["sni_hostname"] == "public.example"
        with pytest.raises(FetchError, match="rate limited") as limited:
            await client.get("https://public.example/limited")
        assert limited.value.category == "rate_limited"
        assert limited.value.retry_after == 13
    finally:
        await client._client.aclose()
