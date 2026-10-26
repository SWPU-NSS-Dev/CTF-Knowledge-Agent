# CTF-Knowledge-Agent — stage 1 architecture

Local-first, independently implemented Python 3.11+ application. Stage 1 delivers actual public-source crawling, immutable local snapshots, versioned SQLite storage, persistent crawl jobs, offline search, CLI, and FastAPI. AI/RAG/AWDP and a complete management UI belong to subsequent stages; do not publish nonfunctional endpoints.

## User changes

- No hard-coded crawl page count, article count, source count, lifetime or knowledge-size ceiling. `max_items: null` means exhaust discovery; explicit limits are optional operational controls.
- Throughput is configurable per source and globally; connections are reused and fetching is asynchronous. `requests_per_second: 0` disables deliberate delay. Website responses (429/Retry-After), public access boundaries and data integrity still matter. Do not claim an absolute remote throughput guarantee.
- Websites are plugins, independently discoverable from `ctfka.sources` Python entry points or explicit `module:Class` configuration. Existing registry/worker/API code must not need editing to add a site. Include configurable generic HTML and RSS adapters, CTF Wiki, and public GitHub Markdown/HTML repositories.
- Root agent owns architecture/review; implementation is delegated to GPT-6 Luna. No external paid AI/API dependency.

## Boundaries and shared Python contract

All package paths below are relative to repository root. Use `ctfka/` directly. The storage implementer owns `ctfka/config.py`, `ctfka/contracts.py`, `ctfka/models/`, `ctfka/ingestion/`, `ctfka/indexing/`, their tests. Crawler implementer owns `ctfka/crawler/`, `ctfka/scheduler/`, `ctfka/security/`, crawler fixtures/tests. API implementer owns `ctfka/api/`, `ctfka/cli/`, packaging, CLI/API tests, README, configuration examples, CI. Communicate contract additions before changing shared interfaces.

`ctfka.config.Settings` is a Pydantic model with `data_dir: Path`, `sources: dict[str, SourceConfig]`, `workers: int = 8`, `token: str | None = None`, `host='127.0.0.1'`, `port=8000`. `Settings.load(path: str | Path | None = None)` reads YAML if present and CTFKA_DATA_DIR / CTFKA_TOKEN environment overrides. `SourceConfig` fields: `adapter: str`, `enabled: bool=True`, `options: dict`, `concurrency: int=4`, `requests_per_second: float=0`, `max_items: int | None=None`, `max_attempts: int=5`, `timeout_seconds: float=30`, `max_response_bytes: int | None=33554432`. Bounds must validate; zero rate means no artificial pacing. Independent adapters can define options without central schema modification.

`ctfka.contracts` dataclasses:
- `DiscoveredItem(url: str, identity: str, metadata: dict = field(default_factory=dict))`
- `FetchedDocument(url: str, raw: bytes, content_type: str='text/markdown', metadata: dict=field(default_factory=dict))`
- `ParsedDocument(title: str, content: str, metadata: dict=field(default_factory=dict))`

`ctfka.models.store.Store(data_dir: str | Path)` creates directories, initializes schema through versioned migration(s), SQLite WAL/FTS5 and immutable raw snapshots. Methods use short-lived connections suitable for threads; serialized write transactions, no shared thread-unsafe connection:
- `register_source(name: str, adapter: str, config: dict | None=None) -> None`
- `ingest(source: str, document: FetchedDocument, parsed: ParsedDocument | None=None) -> dict` returns article (with id, version, raw_path, duplicate info), deterministic URL/source identity, SHA hashes, versions and near-duplicate candidate metadata; parser runs if parsed omitted. Store raw before transactional article/index update; never delete existing snapshots on errors.
- `search(query: str, limit: int=20, source: str | None=None) -> list[dict]`: id, title, url, source, raw_path, snippet, content/version/evidence offsets. Preserve technical symbols and Chinese matching using FTS5 plus literal-symbol/Chinese indexing as needed, escape query input.
- `list_articles(limit: int=50, offset: int=0) -> list[dict]`, `get_article(article_id: str) -> dict | None` (includes normalized content and version history), `stats() -> dict`, `rebuild_index() -> dict`.
- `enqueue_job(source: str, item: DiscoveredItem, priority: int=0) -> dict` (durable idempotence while active; completed jobs can be refreshed during later discovery to detect versions).
- `claim_job(source: str | None=None, lease_seconds: float=300) -> dict | None` atomically returns id, source, url, identity, metadata, attempts; releases expired leases.
- `complete_job(job_id: str, result: dict | None=None) -> None`
- `fail_job(job_id: str, error: str, category: str, retry_after: float | None=None, max_attempts: int=5) -> None`: permanent blocked/failed versus retry_waiting, exponential jitter, honor provided retry delay.
- `retry_job(job_id: str) -> bool`, `list_jobs(limit: int=100, status: str | None=None) -> list[dict]`, `job_stats() -> dict`, `recover_jobs() -> int` (only expired running leases), `next_retry_delay() -> float | None` (until nearest queued retry).
- `get_source_state(source: str) -> dict`, `set_source_state(source: str, state: dict) -> None`, `archive_missing(source: str, identities: list[str]) -> int` marks known articles absent from complete repository scan archived, never after partial discovery.
- `backup(destination: str | Path | None=None) -> str`, `restore(archive: str | Path) -> dict` (manifest SHA validation + archive traversal protection).

`ctfka.ingestion.parser.parse_document(document: FetchedDocument) -> ParsedDocument` handles Markdown / HTML body extraction with code/table/title/link fidelity. Local directory importer uses the same ingest path. Stage 1 only promises Markdown/HTML; PDF/AI explicitly deferred.

`ctfka.crawler.registry.AdapterRegistry()` discovers built-ins (`ctfwiki`, `github`, `html`, `rss`) and entry points; `.create(name: str, config: SourceConfig, client: FetchClient) -> SourceAdapter`, `.names() -> list[str]`. Explicit `module:Class` imports trusted installed Python plugins; article content cannot register plugins. Adapter constructor `(config: SourceConfig, client: FetchClient)`.

`SourceAdapter` ABC exposes `async discover(cursor: dict | None=None) -> AsyncIterator[DiscoveredItem]`, `async fetch(item) -> FetchedDocument`, `parse(document) -> ParsedDocument`, `normalize(parsed) -> ParsedDocument`, `validate(parsed) -> None` (raise invalid content), `state: dict`. Adapter state is persisted only after successful complete discovery and after all data needed for incremental operation are durable; avoid updating commit cursor before successful fetched jobs.

`ctfka.crawler.http.FetchClient(config: SourceConfig)` async context manager exposes `async get(url: str, headers: dict | None=None) -> httpx.Response`. Reuse client pool, bound concurrency, validate public HTTP(S) URLs and every redirect, respect robots, enforce configurable response bytes/timeouts, classify errors. Conditional requests use cached metadata; a cold-cache 304 cannot be ingested as empty. Worker handles external server errors honestly. No execution tools.

`ctfka.scheduler.worker.CrawlService(settings: Settings, store: Store)` exposes `async discover(source: str) -> dict`, `async run(source: str | None=None, max_jobs: int | None=None) -> dict`, `async crawl(source: str, max_jobs: int | None=None) -> dict` (discover then run), `async close() -> None`. Worker must maintain source concurrency and release leases safely on cancellation; startup only reclaims expired leases. Long jobs heartbeat lease if needed. No spawned shell or exploit execution.

## API / CLI acceptance

`ctfka.api.app.create_app(settings: Settings | None=None) -> FastAPI`: /health, /api/v1/search, /api/v1/articles, /api/v1/articles/{id}, /api/v1/sources (list/config upsert), /api/v1/crawl/jobs POST/GET, /api/v1/crawl/status, /api/v1/crawl/jobs/{id}/retry, /api/v1/stats, /api/v1/index/rebuild, /api/v1/backup. Lifespan manages worker resources/tasks. Administrative endpoints require bearer token (init generates local token; missing token disables admin). Explicit error codes and no fake RAG endpoints. Basic local root help page optional, no full UI requirement at this stage.

CLI: `ctfka init`, `crawl --source NAME`, `import PATH`, `index build`, `search QUERY`, `serve`, `backup create`, `backup restore PATH`, plus `sources list` / `jobs list` / `jobs retry ID`. Global --config and --data-dir; JSON output for commands. Defaults discover usable public sources with unrestricted discovery count; live acceptance passes explicit small operational limits to avoid downloading entire corpora during tests.

## Verification

Offline pytest + Ruff; real-format fixtures, version/dedupe/restart/retry/Chinese/symbol/API/auth/plugin extensibility/backup verification. Run actual bounded public CTF Wiki + GitHub ingestion and record environment, counts, timings and search hits without inventing success. Keep data, .venv, secrets outside version control. README and stage report state exactly what works and what is deferred.
