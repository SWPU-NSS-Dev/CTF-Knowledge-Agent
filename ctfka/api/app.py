from __future__ import annotations

import asyncio
import secrets
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from ctfka.config import Settings, SourceConfig
from ctfka.models.store import Store


class SourceRequest(BaseModel):
    adapter: str
    enabled: bool = True
    options: dict[str, Any] = Field(default_factory=dict)
    concurrency: int = Field(default=4, ge=1)
    requests_per_second: float = Field(default=0, ge=0)
    max_items: int | None = Field(default=None, ge=1)
    max_attempts: int = Field(default=5, ge=1)
    timeout_seconds: float = Field(default=30, gt=0)
    max_response_bytes: int | None = Field(default=33554432, ge=1)


class CrawlRequest(BaseModel):
    source: str
    max_jobs: int | None = Field(default=None, ge=1)
    max_items: int | None = Field(default=None, ge=1)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.load()
    store = Store(settings.data_dir)
    # YAML wins for names it declares; API-created source settings persist in SQLite.
    for name, config in settings.sources.items():
        store.register_source(name, config.adapter, config.model_dump())
    for persisted in store.list_sources():
        name = persisted["name"]
        if persisted.get("adapter") == "local":
            continue
        if name not in settings.sources and persisted.get("config"):
            try:
                settings.sources[name] = SourceConfig.model_validate(persisted["config"])
            except (TypeError, ValueError):
                continue
    crawl_tasks: set[asyncio.Task[Any]] = set()
    crawl_runs: dict[str, dict[str, Any]] = {}
    active_sources: set[str] = set()
    worker_state: dict[str, Any] = {"status": "stopped", "error": None}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        from ctfka.scheduler.worker import CrawlService

        service = CrawlService(settings, store)
        app.state.settings, app.state.store, app.state.crawl_service = settings, store, service
        app.state.crawl_runs = crawl_runs
        store.recover_jobs()

        async def supervise() -> None:
            worker_state.update(status="running", error=None)
            try:
                result = await service.run_forever()
                worker_state.update(status="stopped", result=result)
            except asyncio.CancelledError:
                worker_state["status"] = "stopped"
                raise
            except Exception as exc:  # noqa: BLE001 - expose fatal worker errors in status.
                worker_state.update(status="failed", error=f"{type(exc).__name__}: {exc}")

        worker_task = asyncio.create_task(supervise(), name="ctfka-crawl-worker")
        app.state.worker_task = worker_task
        app.state.worker_state = worker_state
        try:
            yield
        finally:
            for task in tuple(crawl_tasks):
                task.cancel()
            if crawl_tasks:
                await asyncio.gather(*crawl_tasks, return_exceptions=True)
            worker_task.cancel()
            await asyncio.gather(worker_task, return_exceptions=True)
            await service.close()

    app = FastAPI(title="CTF Knowledge Agent", version="0.1.0", lifespan=lifespan)
    app.state.settings, app.state.store = settings, store
    app.state.crawl_runs = crawl_runs
    app.state.worker_state = worker_state

    def launch_run(source: str | None, max_jobs: int | None, max_items: int | None = None) -> str:
        if source is not None and source in active_sources:
            raise HTTPException(status_code=409, detail="A crawl for this source is already active")
        run_id = uuid.uuid4().hex
        crawl_runs[run_id] = {
            "id": run_id,
            "source": source,
            "max_jobs": max_jobs,
            "max_items": max_items,
            "status": "running",
            "error": None,
        }
        if source is not None:
            active_sources.add(source)

        async def execute() -> None:
            try:
                service = app.state.crawl_service
                if max_items is None:
                    result = await service.crawl(source, max_jobs=max_jobs)
                else:
                    original = settings.sources[source]
                    bounded = original.model_copy(update={"max_items": max_items})
                    await service.refresh_source(source, bounded)
                    try:
                        discovery = await service.discover(source)
                    finally:
                        await service.refresh_source(source, original)
                    result = {"source": source, "discovery": discovery, "run": await service.run(source, max_jobs)}
                run_result = result.get("run") or {}
                errors = list(run_result.get("errors") or [])
                if run_result.get("failed", 0) > 0 or errors:
                    crawl_runs[run_id].update(
                        status="completed_with_errors",
                        result=result,
                        errors=errors,
                    )
                else:
                    crawl_runs[run_id].update(status="completed", result=result)
            except asyncio.CancelledError:
                crawl_runs[run_id]["status"] = "cancelled"
                raise
            except Exception as exc:  # noqa: BLE001 - preserve run errors in the status API.
                crawl_runs[run_id].update(status="failed", error=str(exc))
            finally:
                if source is not None:
                    active_sources.discard(source)

        task = asyncio.create_task(execute())
        crawl_tasks.add(task)
        task.add_done_callback(crawl_tasks.discard)
        return run_id

    async def admin_auth(authorization: str | None = Header(default=None)) -> None:
        if settings.token is None:
            raise HTTPException(status_code=503, detail="Administrative API is disabled until a token is configured")
        scheme, _, supplied = (authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not secrets.compare_digest(supplied, settings.token):
            raise HTTPException(status_code=401, detail="Bearer token required", headers={"WWW-Authenticate": "Bearer"})

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def home() -> str:
        return """<!doctype html><meta charset=utf-8><title>CTF Knowledge Agent</title>
        <h1>CTF Knowledge Agent</h1><p>Local-first source collection and offline search.</p>
        <p>API: <a href='/docs'>/docs</a> · health: <a href='/health'>/health</a></p>
        <p>AI, RAG, AWDP and management UI are planned for later stages.</p>"""

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/v1/search")
    def search(q: str = Query(min_length=1), limit: int = Query(default=20, ge=1), source: str | None = None) -> dict[str, Any]:
        return {"items": store.search(q, limit=limit, source=source)}

    @app.get("/api/v1/articles")
    def articles(limit: int = Query(default=50, ge=1), offset: int = Query(default=0, ge=0)) -> dict[str, Any]:
        return {"items": store.list_articles(limit=limit, offset=offset)}

    @app.get("/api/v1/articles/{article_id}")
    def article(article_id: str) -> dict[str, Any]:
        result = store.get_article(article_id)
        if result is None:
            raise HTTPException(status_code=404, detail="Article not found")
        return result

    @app.get("/api/v1/sources")
    def sources() -> dict[str, Any]:
        return {"items": [{"name": name, **config.model_dump()} for name, config in settings.sources.items()]}

    @app.put("/api/v1/sources/{name}", dependencies=[Depends(admin_auth)])
    async def upsert_source(name: str, request: SourceRequest) -> dict[str, Any]:
        if not name.strip() or len(name) > 128:
            raise HTTPException(status_code=422, detail="Invalid source name")
        # API callers may select installed/discovered adapters, never import arbitrary Python.
        from ctfka.crawler.registry import AdapterRegistry
        registry = AdapterRegistry()
        if request.adapter not in registry.names():
            raise HTTPException(status_code=422, detail="Adapter is not installed or registered")
        config = SourceConfig.model_validate(request.model_dump())
        if name in active_sources or store.job_stats().get("running", 0):
            raise HTTPException(status_code=409, detail="Source configuration cannot change while its crawl is active")
        service = getattr(app.state, "crawl_service", None)
        if service is not None:
            await service.refresh_source(name, config)
        settings.sources[name] = config
        await asyncio.to_thread(store.register_source, name, config.adapter, config.model_dump())
        return {"name": name, **config.model_dump()}

    @app.post("/api/v1/crawl/jobs", status_code=202, dependencies=[Depends(admin_auth)])
    async def start_crawl(request: CrawlRequest) -> dict[str, Any]:
        if request.source not in settings.sources:
            raise HTTPException(status_code=404, detail="Source not configured")
        config = settings.sources[request.source]
        if not config.enabled:
            raise HTTPException(status_code=409, detail="Source is disabled")
        run_id = launch_run(request.source, request.max_jobs, request.max_items)
        job_stats = await asyncio.to_thread(store.job_stats)
        return {"accepted": True, "run_id": run_id, "source": request.source, "jobs": job_stats}

    @app.get("/api/v1/crawl/jobs")
    def jobs(limit: int = Query(default=100, ge=1), status: str | None = None) -> dict[str, Any]:
        return {"items": store.list_jobs(limit=limit, status=status), "stats": store.job_stats()}

    @app.get("/api/v1/crawl/status")
    def crawl_status() -> dict[str, Any]:
        return {
            "jobs": store.job_stats(),
            "active_requests": len(crawl_tasks),
            "runs": list(crawl_runs.values()),
            "worker": dict(worker_state),
        }

    @app.post("/api/v1/crawl/jobs/{job_id}/retry", dependencies=[Depends(admin_auth)])
    async def retry_job(job_id: str) -> dict[str, Any]:
        if not await asyncio.to_thread(store.retry_job, job_id):
            raise HTTPException(status_code=409, detail="Job cannot be retried")
        service = getattr(app.state, "crawl_service", None)
        if service is not None:
            await service.wake()
        return {"retried": True, "job_id": job_id}

    @app.get("/api/v1/stats")
    def stats() -> dict[str, Any]:
        return {**store.stats(), "jobs": store.job_stats()}

    @app.post("/api/v1/index/rebuild", dependencies=[Depends(admin_auth)])
    def rebuild_index() -> dict[str, Any]:
        return store.rebuild_index()

    @app.post("/api/v1/backup", dependencies=[Depends(admin_auth)])
    def backup(destination: str | None = None) -> dict[str, str]:
        path = Path(destination).expanduser() if destination else None
        if path is not None:
            root = settings.data_dir.expanduser().resolve()
            path = path.resolve()
            if root not in path.parents:
                raise HTTPException(status_code=422, detail="Backup destination must be inside data_dir")
        return {"path": store.backup(path)}

    return app
