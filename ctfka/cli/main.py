from __future__ import annotations

import asyncio
import json
import secrets
from pathlib import Path
from typing import Any

import typer

from ctfka.config import Settings
from ctfka.models.store import Store

app = typer.Typer(help="Local-first CTF knowledge collection and search", no_args_is_help=True)
sources_app = typer.Typer(help="Manage configured sources", no_args_is_help=True)
jobs_app = typer.Typer(help="Inspect and retry durable crawl jobs", no_args_is_help=True)
backup_app = typer.Typer(help="Create or restore store backups", no_args_is_help=True)
index_app = typer.Typer(help="Build the offline search index", no_args_is_help=True)
models_app = typer.Typer(help="Inspect local language models", no_args_is_help=True)
knowledge_app = typer.Typer(help="Inspect extracted knowledge cards", no_args_is_help=True)
app.add_typer(sources_app, name="sources")
app.add_typer(jobs_app, name="jobs")
app.add_typer(backup_app, name="backup")
app.add_typer(index_app, name="index")
app.add_typer(models_app, name="models")
app.add_typer(knowledge_app, name="knowledge")


def _settings(config: Path | None, data_dir: Path | None) -> Settings:
    selected_config = config
    if selected_config is None:
        selected_config = next((candidate for candidate in (Path("config.local.yaml"), Path("config.yaml")) if candidate.is_file()), None)
    settings = Settings.load(selected_config)
    if data_dir is not None:
        settings.data_dir = data_dir
    store = Store(settings.data_dir)
    for persisted in store.list_sources():
        if persisted.get("adapter") == "local" or persisted["name"] in settings.sources or not persisted.get("config"):
            continue
        try:
            from ctfka.config import SourceConfig
            settings.sources[persisted["name"]] = SourceConfig.model_validate(persisted["config"])
        except (TypeError, ValueError):
            continue
    return settings


def _json(value: Any) -> None:
    typer.echo(json.dumps(value, ensure_ascii=False, indent=2, default=str))


@app.callback()
def main(
    ctx: typer.Context,
    config: Path | None = typer.Option(None, "--config", help="YAML settings file"),
    data_dir: Path | None = typer.Option(None, "--data-dir", help="Override the local data directory"),
) -> None:
    ctx.obj = _settings(config, data_dir)
    ctx.meta["config_path"] = config


@app.command()
def init(ctx: typer.Context, force: bool = typer.Option(False, "--force", help="Overwrite existing config")) -> None:
    """Create a local config and generate an admin bearer token."""
    path = Path(ctx.find_root().meta.get("config_path") or "config.local.yaml").expanduser()
    if path.exists() and not force:
        raise typer.BadParameter(f"{path} already exists; pass --force to replace it")
    try:
        import yaml
    except ImportError as exc:
        typer.echo("PyYAML is required", err=True)
        raise typer.Exit(2) from exc
    defaults = {
        "data_dir": "./data", "workers": 8, "token": secrets.token_urlsafe(32),
        "host": "127.0.0.1", "port": 8000,
        "sources": {
            "ctfwiki": {"adapter": "ctfwiki", "enabled": True, "max_items": None, "options": {}},
            "github": {"adapter": "github", "enabled": False, "max_items": None, "options": {}},
        },
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(defaults, allow_unicode=True, sort_keys=False), encoding="utf-8")
    _json({"config": str(path.resolve()), "data_dir": str(Path(defaults["data_dir"]).resolve()), "token": defaults["token"]})


@app.command()
def crawl(
    ctx: typer.Context,
    source: str = typer.Option(..., "--source"),
    max_jobs: int | None = typer.Option(None, "--max-jobs", min=1),
    max_items: int | None = typer.Option(None, "--max-items", min=1),
) -> None:
    """Discover and fetch articles from a configured source."""
    settings = ctx.obj
    if source not in settings.sources:
        raise typer.BadParameter(f"Source {source!r} is not configured")
    if max_items is not None:
        settings.sources[source] = settings.sources[source].model_copy(update={"max_items": max_items})
    from ctfka.scheduler.worker import CrawlService
    store = Store(settings.data_dir)
    async def run() -> dict[str, Any]:
        service = CrawlService(settings, store)
        try:
            return await service.crawl(source, max_jobs=max_jobs)
        finally:
            await service.close()
    result = asyncio.run(run())
    _json(result)
    run_result = result.get("run", {})
    if run_result.get("failed", 0) > 0 or run_result.get("errors"):
        raise typer.Exit(1)


@app.command()
def worker(ctx: typer.Context, source: str | None = typer.Option(None, "--source")) -> None:
    """Continuously process queued and due retry jobs; Ctrl+C stops the worker."""
    from ctfka.scheduler.worker import CrawlService
    settings = ctx.obj
    store = Store(settings.data_dir)

    async def run() -> dict[str, Any]:
        service = CrawlService(settings, store)
        try:
            return await service.run_forever(source=source)
        finally:
            await service.close()

    _json(asyncio.run(run()))


@app.command("import")
def import_path(ctx: typer.Context, path: Path = typer.Argument(..., exists=True, readable=True)) -> None:
    """Import local Markdown and HTML files through normal ingestion."""
    from ctfka.contracts import FetchedDocument
    from ctfka.ingestion.parser import parse_document
    store = Store(ctx.obj.data_dir)
    store.register_source("local-import", "local", {})
    paths = [path] if path.is_file() else sorted(p for p in path.rglob("*") if p.is_file() and p.suffix.lower() in {".md", ".markdown", ".html", ".htm"})
    results = []
    for file in paths:
        kind = "text/html" if file.suffix.lower() in {".html", ".htm"} else "text/markdown"
        document = FetchedDocument(url=file.resolve().as_uri(), raw=file.read_bytes(), content_type=kind, metadata={"title": file.stem})
        results.append(store.ingest("local-import", document, parse_document(document)))
    _json({"imported": len(results), "items": results})


@index_app.command("build")
def index_build(ctx: typer.Context, semantic: bool = typer.Option(False, "--semantic/--no-semantic")) -> None:
    settings = ctx.obj
    from ctfka.indexing.service import IndexService
    from ctfka.llm.ollama import OllamaProvider
    async def run() -> dict[str, Any]:
        provider = OllamaProvider(settings.llm)
        try:
            return await IndexService(settings, Store(settings.data_dir), provider).build(semantic=semantic)
        finally:
            await provider.close()
    _json(asyncio.run(run()))


@app.command()
def search(ctx: typer.Context, query: str = typer.Argument(...), limit: int = typer.Option(20, min=1), source: str | None = typer.Option(None, "--source"), mode: str = typer.Option("keyword", "--mode", help="keyword or hybrid")) -> None:
    if mode not in {"keyword", "hybrid"}:
        raise typer.BadParameter("mode must be keyword or hybrid")
    settings = ctx.obj
    store = Store(settings.data_dir)
    if mode == "keyword":
        _json({"items": store.search(query, limit=limit, source=source)})
        return
    from ctfka.llm.ollama import OllamaProvider
    from ctfka.retrieval.service import RetrievalService
    async def run() -> dict[str, Any]:
        provider = OllamaProvider(settings.llm)
        try:
            return await RetrievalService(settings, store, provider).search(query, limit=limit, source=source, semantic=True)
        finally:
            await provider.close()
    _json(asyncio.run(run()))


@models_app.command("status")
def models_status(ctx: typer.Context) -> None:
    from ctfka.llm.ollama import OllamaProvider
    async def run() -> dict[str, Any]:
        provider = OllamaProvider(ctx.obj.llm)
        try:
            return await provider.status()
        finally:
            await provider.close()
    _json(asyncio.run(run()))


@app.command()
def chat(ctx: typer.Context, question: str | None = typer.Argument(None)) -> None:
    """Ask the local RAG assistant; omit QUESTION for interactive chat."""
    from ctfka.llm.ollama import OllamaProvider
    from ctfka.retrieval.rag import RAGService
    settings = ctx.obj
    store = Store(settings.data_dir)
    async def ask(messages: list[dict[str, str]]) -> dict[str, Any]:
        provider = OllamaProvider(settings.llm)
        try:
            return await RAGService(settings, store, provider).answer(messages)
        finally:
            await provider.close()
    if question:
        _json(asyncio.run(ask([{"role": "user", "content": question}])))
        return
    history: list[dict[str, str]] = []
    while True:
        try:
            value = typer.prompt("你", prompt_suffix=" > ")
        except (EOFError, KeyboardInterrupt):
            break
        if value.strip().lower() in {"exit", "quit", "退出"}:
            break
        history.append({"role": "user", "content": value})
        result = asyncio.run(ask(history))
        typer.echo(result.get("answer", ""))
        history.append({"role": "assistant", "content": result.get("answer", "")})
        _json({key: result.get(key) for key in ("citations", "grounded", "warnings")})


@app.command()
def extract(ctx: typer.Context, article_id: str = typer.Argument(...)) -> None:
    from ctfka.llm.ollama import OllamaProvider
    from ctfka.processing.extraction import KnowledgeExtractor
    settings = ctx.obj
    store = Store(settings.data_dir)
    async def run() -> dict[str, Any]:
        provider = OllamaProvider(settings.llm)
        try:
            return await KnowledgeExtractor(settings, store, provider).extract(article_id)
        finally:
            await provider.close()
    _json(asyncio.run(run()))


@knowledge_app.command("list")
def knowledge_list(ctx: typer.Context, limit: int = typer.Option(50, min=1), offset: int = typer.Option(0, min=0)) -> None:
    _json({"items": Store(ctx.obj.data_dir).list_knowledge(limit=limit, offset=offset)})


@knowledge_app.command("show")
def knowledge_show(ctx: typer.Context, knowledge_id: str = typer.Argument(...)) -> None:
    result = Store(ctx.obj.data_dir).get_knowledge(knowledge_id)
    if result is None:
        raise typer.BadParameter("Knowledge card not found")
    _json(result)


@app.command()
def serve(ctx: typer.Context, host: str | None = typer.Option(None), port: int | None = typer.Option(None, min=1, max=65535)) -> None:
    """Run the local FastAPI service."""
    import uvicorn
    settings = ctx.obj
    from ctfka.api.app import create_app
    uvicorn.run(create_app(settings), host=host or settings.host, port=port or settings.port)


@backup_app.command("create")
def backup_create(ctx: typer.Context, destination: Path | None = typer.Argument(None)) -> None:
    _json({"path": Store(ctx.obj.data_dir).backup(destination)})


@backup_app.command("restore")
def backup_restore(ctx: typer.Context, path: Path = typer.Argument(..., exists=True, readable=True)) -> None:
    _json(Store(ctx.obj.data_dir).restore(path))


@sources_app.command("list")
def sources_list(ctx: typer.Context) -> None:
    _json({"items": [{"name": name, **config.model_dump()} for name, config in ctx.obj.sources.items()]})


@jobs_app.command("list")
def jobs_list(ctx: typer.Context, limit: int = typer.Option(100, min=1), status: str | None = typer.Option(None)) -> None:
    store = Store(ctx.obj.data_dir)
    _json({"items": store.list_jobs(limit=limit, status=status), "stats": store.job_stats()})


@jobs_app.command("retry")
def jobs_retry(ctx: typer.Context, job_id: str = typer.Argument(...)) -> None:
    success = Store(ctx.obj.data_dir).retry_job(job_id)
    if not success:
        raise typer.BadParameter("Job cannot be retried")
    _json({"retried": True, "job_id": job_id})


if __name__ == "__main__":
    app()
