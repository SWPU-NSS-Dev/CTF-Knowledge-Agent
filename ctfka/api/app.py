from __future__ import annotations

import asyncio
import json
import secrets
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from ctfka.config import Settings, SourceConfig
from ctfka.models.store import Store


def _service_error(exc: Exception) -> HTTPException:
    try:
        from ctfka.llm.base import LLMTimeout, LLMUnavailable, ModelNotFound
        if isinstance(exc, ModelNotFound):
            return HTTPException(status_code=404, detail=str(exc))
        if isinstance(exc, LLMTimeout):
            return HTTPException(status_code=504, detail=str(exc))
        if isinstance(exc, LLMUnavailable):
            return HTTPException(status_code=503, detail=str(exc))
    except ImportError:
        pass
    return HTTPException(status_code=502, detail=f"Local AI request failed: {type(exc).__name__}: {exc}")


def _openai_completion(result: dict[str, Any]) -> dict[str, Any]:
    usage = result.get("usage") or {}
    finish_reason = "length" if result.get("finish_reason") == "length" else "stop"
    payload = {
        "id": "chatcmpl-" + uuid.uuid4().hex,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": result.get("model"),
        "choices": [{"index": 0, "message": {"role": "assistant", "content": result.get("answer", "")}, "finish_reason": finish_reason}],
        "usage": {key: usage[key] for key in ("prompt_tokens", "completion_tokens", "total_tokens") if key in usage and usage[key] is not None},
        "citations": result.get("citations", []),
        "retrieval_mode": result.get("retrieval_mode"),
        "warnings": result.get("warnings", []),
        "grounded": result.get("grounded", False),
        "inference_status": result.get("inference_status", "ai_inferred"),
    }
    return payload


def _sse(payload: dict[str, Any]) -> str:
    return "data: " + json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n\n"


def _normalize_model_name(name: str) -> str:
    final_component = name.rsplit("/", 1)[-1]
    return name if ":" in final_component else f"{name}:latest"


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


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: str
    content: str


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: str | None = None
    messages: list[ChatMessage]
    stream: bool = False
    n: int = Field(default=1, ge=1)
    temperature: float | None = Field(default=None, ge=0)
    top_p: float | None = Field(default=None, gt=0, le=1)
    max_tokens: int | None = Field(default=None, ge=1)


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
        from ctfka.indexing.service import IndexService
        from ctfka.llm.ollama import OllamaProvider
        from ctfka.processing.extraction import KnowledgeExtractor
        from ctfka.retrieval.rag import RAGService
        from ctfka.scheduler.worker import CrawlService

        service = CrawlService(settings, store)
        provider = OllamaProvider(settings.llm)
        app.state.settings, app.state.store, app.state.crawl_service = settings, store, service
        app.state.provider = provider
        app.state.rag_service = RAGService(settings, store, provider)
        app.state.extractor = KnowledgeExtractor(settings, store, provider)
        app.state.index_service = IndexService(settings, store, provider)
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
            await provider.close()

    app = FastAPI(title="CTF Knowledge Agent", version="0.2.0", lifespan=lifespan)
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
        <h1>CTF Knowledge Agent</h1><p>Local-first source collection, hybrid retrieval and local AI.</p>
        <p>API: <a href='/docs'>/docs</a> · health: <a href='/health'>/health</a></p>
        <p>API: <code>/v1/chat/completions</code> · <code>/api/v1/knowledge</code></p>"""

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/v1/search")
    async def search(q: str = Query(min_length=1), limit: int = Query(default=20, ge=1), source: str | None = None, mode: str = Query(default="keyword", pattern="^(keyword|hybrid)$")) -> dict[str, Any]:
        if mode == "keyword":
            return {"items": await asyncio.to_thread(store.search, q, limit=limit, source=source)}
        from ctfka.retrieval.service import RetrievalService
        try:
            return await RetrievalService(settings, store, app.state.provider).search(q, limit=limit, source=source, semantic=True)
        except Exception as exc:
            raise _service_error(exc) from exc

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
    async def rebuild_index(semantic: bool = Query(default=False)) -> dict[str, Any]:
        article_index = await asyncio.to_thread(store.rebuild_index)
        index_service = getattr(app.state, "index_service", None)
        temporary_provider = None
        try:
            if index_service is None:
                from ctfka.indexing.service import IndexService
                from ctfka.llm.ollama import OllamaProvider
                temporary_provider = OllamaProvider(settings.llm)
                index_service = IndexService(settings, store, temporary_provider)
            chunk_index = await index_service.build(semantic=semantic)
        except Exception as exc:
            raise _service_error(exc) from exc
        finally:
            if temporary_provider is not None:
                await temporary_provider.close()
        return {"articles": article_index, "chunks": chunk_index}

    @app.post("/api/v1/index/build", dependencies=[Depends(admin_auth)])
    async def index_build(semantic: bool = Query(default=False)) -> dict[str, Any]:
        try:
            return await app.state.index_service.build(semantic=semantic)
        except Exception as exc:
            raise _service_error(exc) from exc

    @app.get("/api/v1/models/status")
    async def model_status() -> dict[str, Any]:
        return await app.state.provider.status()

    @app.get("/v1/models")
    async def models() -> dict[str, Any]:
        status = await app.state.provider.status()
        if not status.get("reachable"):
            raise HTTPException(status_code=503, detail=status.get("error") or "Local model service unavailable")
        return {"object": "list", "data": [{"id": item["name"], "object": "model", "created": 0, "owned_by": "ollama"} for item in await app.state.provider.list_models()]}

    @app.get("/api/v1/knowledge")
    def knowledge(limit: int = Query(default=50, ge=1), offset: int = Query(default=0, ge=0)) -> dict[str, Any]:
        return {"items": store.list_knowledge(limit=limit, offset=offset)}

    @app.get("/api/v1/knowledge/{knowledge_id}")
    def knowledge_item(knowledge_id: str) -> dict[str, Any]:
        result = store.get_knowledge(knowledge_id)
        if result is None:
            raise HTTPException(status_code=404, detail="Knowledge card not found")
        return result

    @app.post("/api/v1/articles/{article_id}/extract", dependencies=[Depends(admin_auth)])
    async def extract(article_id: str) -> dict[str, Any]:
        if await asyncio.to_thread(store.get_article, article_id) is None:
            raise HTTPException(status_code=404, detail="Article not found")
        try:
            return await app.state.extractor.extract(article_id)
        except Exception as exc:
            raise _service_error(exc) from exc

    @app.post("/v1/chat/completions", dependencies=[Depends(admin_auth)])
    @app.post("/api/v1/chat/completions", dependencies=[Depends(admin_auth)], include_in_schema=False)
    async def chat(request: Request, body: ChatRequest) -> Any:
        messages = [message.model_dump() for message in body.messages]
        if not messages or any(message["role"] not in {"system", "user", "assistant"} for message in messages):
            raise HTTPException(status_code=422, detail="messages must contain text with system, user, or assistant roles")
        if body.n != 1:
            raise HTTPException(status_code=422, detail="n greater than 1 is not supported")
        options = {key: value for key, value in {"temperature": body.temperature, "top_p": body.top_p, "num_predict": body.max_tokens}.items() if value is not None}
        provider = app.state.provider
        try:
            # Resolve unavailable service/model before HTTP streaming headers are sent.
            status = await provider.status()
            if not status.get("reachable"):
                raise HTTPException(status_code=503, detail=status.get("error") or "Local model service unavailable")
            model = _normalize_model_name(body.model or settings.llm.chat_model)
            models_found = await provider.list_models()
            if model not in {item.get("name") for item in models_found}:
                raise HTTPException(status_code=404, detail=f"Model {model!r} is not installed")
        except HTTPException:
            raise
        except Exception as exc:
            raise _service_error(exc) from exc
        if not body.stream:
            try:
                result = await app.state.rag_service.answer(messages, model=model, options=options)
            except Exception as exc:
                raise _service_error(exc) from exc
            return _openai_completion(result)

        async def events():
            completion_id = "chatcmpl-" + uuid.uuid4().hex
            created = int(time.time())
            model_name = model
            yield _sse({"id": completion_id, "object": "chat.completion.chunk", "created": created, "model": model_name, "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]})
            iterator = app.state.rag_service.stream(messages, model=model, options=options).__aiter__()
            sent_content = False
            result_received = False
            finish_reason = "stop"
            async def watch_disconnect() -> None:
                while not await request.is_disconnected():
                    await asyncio.sleep(0.1)
            disconnect_task = asyncio.create_task(watch_disconnect())
            next_event: asyncio.Task[Any] | None = None
            try:
                while True:
                    next_event = asyncio.create_task(iterator.__anext__())
                    done, _ = await asyncio.wait({next_event, disconnect_task}, return_when=asyncio.FIRST_COMPLETED)
                    if disconnect_task in done:
                        next_event.cancel()
                        await asyncio.gather(next_event, return_exceptions=True)
                        next_event = None
                        return
                    try:
                        event = next_event.result()
                    except StopAsyncIteration:
                        next_event = None
                        break
                    next_event = None
                    if event.get("type") == "delta":
                        sent_content = True
                        yield _sse({"id": completion_id, "object": "chat.completion.chunk", "created": created, "model": model_name, "choices": [{"index": 0, "delta": {"content": event.get("content", "")}, "finish_reason": None}]})
                    elif event.get("type") == "result":
                        result = event["result"]
                        result_received = True
                        finish_reason = "length" if result.get("finish_reason") == "length" else "stop"
                        if not sent_content and result.get("answer"):
                            sent_content = True
                            yield _sse({"id": completion_id, "object": "chat.completion.chunk", "created": created, "model": result.get("model", model_name), "choices": [{"index": 0, "delta": {"content": result["answer"]}, "finish_reason": None}]})
                        yield _sse({"id": completion_id, "object": "chat.completion.chunk", "created": created, "model": result.get("model", model_name), "choices": [], "citations": result.get("citations", []), "retrieval_mode": result.get("retrieval_mode"), "grounded": result.get("grounded"), "warnings": result.get("warnings", []), "inference_status": result.get("inference_status"), "usage": result.get("usage")})
                        model_name = result.get("model", model_name)
                if not result_received:
                    raise RuntimeError("RAG stream ended without a final result")
                yield _sse({"id": completion_id, "object": "chat.completion.chunk", "created": created, "model": model_name, "choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}]})
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - preserve named post-header errors as SSE frames.
                yield "event: error\n" + _sse({"error": {"type": type(exc).__name__, "message": str(exc)}})
            finally:
                if next_event is not None and not next_event.done():
                    next_event.cancel()
                    await asyncio.gather(next_event, return_exceptions=True)
                disconnect_task.cancel()
                await asyncio.gather(disconnect_task, return_exceptions=True)
                closer = getattr(iterator, "aclose", None)
                if closer:
                    await closer()
            yield "data: [DONE]\n\n"
        return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

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
