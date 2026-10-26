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
