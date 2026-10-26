# Stage 2 architecture and integration contract

Implement local AI/RAG on the tested stage 1 repository. Root owns architecture, integration review, acceptance and reports; GPT-6 Luna implements application code. Preserve all stage 1 behavior, immutable originals, migrations, backups and site plugin independence. No cloud API or paid provider is needed.

## File ownership

- storage agent: `ctfka/config.py`, `ctfka/models/`, `ctfka/processing/chunking.py`, `ctfka/indexing/`, storage/chunk/vector tests. It owns new shared config definitions first.
- rag agent: `ctfka/llm/`, `ctfka/retrieval/`, `ctfka/processing/extraction.py`, provider/retrieval/RAG/extraction tests. It owns provider and RAG result contracts below.
- api agent: `ctfka/api/`, `ctfka/cli/`, packaging/version, config examples, README, CI, API/CLI tests and docs other than this architecture and the final stage report.

Communicate interface additions before editing another agent's files. No implementation placeholders or fake model output. Qdrant is optional and not required: SQLite vectors with cosine search are sufficient for this local stage if measured honestly.

## Shared configuration (storage creates immediately)

Extend `Settings` without breaking existing configs:

`LLMConfig`: provider='ollama', base_url='http://127.0.0.1:11434', chat_model='qwen3:4b', embedding_model='qwen3-embedding:0.6b', timeout_seconds=120 (positive configurable), max_context_tokens=8192 (positive or None), options:dict={}, embedding_options:dict={}, embedding_dimensions:int|None=None, keep_alive:str='5m', think:bool=True. Acceptance updated the thinking default: the installed qwen3:4b digest reports only `thinking.values=[true]`; `think=false` does not suppress reasoning with this runtime/model combination. The provider forwards the configured control and consumes `message.content`, never forwards the separate `message.thinking` field as an answer. Select a model advertising false if disabling thinking is required.

`RetrievalConfig`: keyword_top_k=30, vector_top_k=30, final_top_k=8, rrf_k=60, use_reranker=False (reject true until implemented), semantic_enabled=True, context_chars=24000 (positive or None), embedding_batch_size=8. All defaults configurable; avoid arbitrary fixed upper bounds.

`ChunkConfig`: max_chars=1800 (positive). `Settings.llm`, `.retrieval`, `.chunking` use default factories. CTFKA_OLLAMA_BASE_URL / CTFKA_CHAT_MODEL / CTFKA_EMBEDDING_MODEL environment overrides supported.

## Chunks, storage and vectors

Acceptance amendment: `LLMConfig.structured_think: bool=False` controls requests with a non-null `format` separately from normal chat's `think`. Real qwen3:4b schema output completed with false; true exhausted the generation budget without JSON. Both options remain explicit configuration, and validation failures remain auditable errors.

`ctfka.processing.chunking.chunk_document(content: str, *, article_id: str, version: int, max_chars: int=1800) -> list[dict]`. Every chunk has id, article_id, version, text, text_hash (SHA256), heading_path:list[str], start, end (character offsets into exact normalized article content), ordinal, previous_id, next_id. Each chunk's text must equal content[start:end]. Prefer heading and Markdown AST block boundaries; never split fenced/indented code. Oversized code blocks remain whole. Preserve all original text and order. IDs deterministic for a given article version and chunk range.

Storage schema migration(s) extend existing SQLite DB with chunks, chunk FTS, embedding manifests/cache, extractions/cards. Use separate modules rather than further expanding the single large Store file where practical. Existing `Store` exposes:

- `sync_chunks(article_id: str | None=None, max_chars: int=1800) -> dict` incrementally creates current chunks; count created/updated/skipped; old versions remain traceable but are excluded from default results. Config fingerprint changes trigger rechunking.
- `list_chunks(article_id: str | None=None, *, current_only: bool=True, limit: int | None=None) -> list[dict]`
- `get_chunk(chunk_id: str) -> dict | None`, enriched with article title/url/source/raw_path, current/version/archive/verification metadata.
- `search_chunks(query: str, limit: int=30, source: str | None=None) -> list[dict]`, current unarchived chunks only; safe FTS BM25 plus exact technical symbol/Chinese matches. Results contain score/snippet and full evidence offsets.
- `get_embeddings(model_key: str, text_hashes: list[str]) -> dict[str,list[float]]`
- `save_embeddings(model_key: str, model: str, digest: str, dimension: int, items: list[dict]) -> None` items text_hash/vector. Enforce finite nonzero same-dimension vectors; reject inconsistent model manifests. Cache is keyed by exact content hash and model identity/config, reusable when unchanged chunks appear in a new article version.
- `vector_search(vector: list[float], model_key: str, limit: int=30, source: str | None=None) -> list[dict]`, join current chunks on text hash; no mixing dimensions/models; cosine ranking.
- `save_extraction(article_id: str, version: int, model: str, model_digest: str, payload: dict, status: str='ai_extracted', error: str | None=None) -> dict`
- `list_knowledge(limit: int=50, offset: int=0) -> list[dict]`, `get_knowledge(id: str) -> dict | None`, `list_extractions(article_id: str | None=None) -> list[dict]`. Cards have evidence offsets, original article/version, model/digest, confidence and unverified status, never human_verified by default. Failed extraction records do not overwrite raw data or delete previous good extractions.

`ctfka.indexing.service.IndexService(settings, store, provider)` has `async build(*, semantic: bool=True, article_id: str | None=None) -> dict`. Always sync chunks/FTS, then batch only missing text hashes. Get provider identity for immutable manifest key using model name, digest and embedding config/dimensions. `.build(semantic=False)` never contacts model. On unavailable provider, return explicit degraded/error status while chunks/FTS remain usable. Surface failed embedding batches and support rerun/resume; never claim complete after a failed batch. Article ingestion need not block on AI: deterministic chunk sync is separate and index build can run after crawl/import. Document this operational flow.

## Provider (rag creates immediately)

`ctfka.llm.base` defines `LLMError`, `LLMUnavailable`, `LLMTimeout`, `ModelNotFound` exceptions and an abstract provider protocol. Ollama implementation `ctfka.llm.ollama.OllamaProvider(config: LLMConfig)` reuses async httpx client, trust_env=False for local service, supports async context or `await close()`:

- `async list_models() -> list[dict]` normalized tags with name/digest/details.
- `async status() -> dict` reachable/version/model names/availability/error (truthful, unavailable is not success).
- `async embedding_identity() -> dict` returns model, digest and model_key (SHA of model/digest/embedding_options/dimensions); never silently use a mutable name as version identity.
- `async embed(texts: list[str]) -> list[list[float]]` calls /api/embed, uses configured model/options/dimensions; validates cardinality/finite/dimension and returns exact vectors.
- `async chat(messages: list[dict], *, model: str | None=None, format: dict | str | None=None, options: dict | None=None) -> dict` returns Ollama native response (message.content, model, token counts). Merge configured runtime options with explicit caller options, token context config, think flag. No cloud fallback or automatic model downloads inside app.
- `stream_chat(messages, *, model=None, options=None) -> AsyncIterator[dict]` yields native Ollama NDJSON objects, including final usage. Enforce a valid terminal done frame, propagate mid-stream errors and cancellation, close HTTP response in finally. An HTTP 200 containing {'error':...} is a failure.

Use official docs: https://docs.ollama.com/api/chat and /api/embed. API tags/version are local. Unknown providers fail clearly. Caller/asyncio timeout or cancellation must close pending response rather than leak generation tasks.

## Hybrid retrieval and RAG (rag owns)

`ctfka.retrieval.service.RetrievalService(settings, store, provider)` has `async search(query, *, limit=None, source=None, semantic=True) -> dict` returning items, mode ('hybrid'/'keyword'), warnings, embedding_model_key. Sync deterministic chunks first, perform chunk BM25/exact matching, optionally query embed only against a matching existing manifest and fuse with RRF. Do not generate an entire corpus's embeddings during each search. If no model or semantic index, fall back to real keyword results. Return evidence metadata and distinguish retrieval scores from truth/confidence.

`ctfka.retrieval.rag.RAGService(settings, store, provider)` owns retrieval and exposes:
- `async answer(messages: list[dict], *, model: str|None=None, options: dict|None=None) -> dict`
- `stream(messages, *, model=None, options=None) -> AsyncIterator[dict]`, internal events: {'type':'delta','content':str}, final {'type':'result','result':<same answer dict>}.

Answer dict: answer:str, model:str, citations:list[dict], usage:dict (prompt_tokens/completion_tokens/total_tokens or null if not known), retrieval_mode, warnings:list[str], grounded:bool, inference_status='ai_inferred'. Citation includes label ('S1'), chunk_id, article_id, article_version, title,url,raw_path,start,end,heading_path,quote,verification_status='unverified'. Query is last user message; include useful chat history without treating source text as higher-priority instructions.

Retrieve → rank → context with stable [S1] labels → local model → citation validation. No evidence: explicit no-evidence answer (deterministic status, not a fake AI answer). Model unavailable with evidence: real retrieved evidence summary and clear model-unavailable degradation, no simulated generated prose. Any cited label must map to supplied exact chunk/version; unknown citations cannot be returned as valid. Require at least one valid evidence label for grounded=true. Grounding validates citation identity/positions, not factual entailment; state this limitation. Do not present inferred facts as human_verified. Long evidence chunks may exceed text target but context assembly must not split code; omit a too-large chunk with explicit warning if it exceeds configured context budget.

Streaming uses actual provider stream. To avoid exposing invalid citation tokens as authoritative, validate at sentence/buffer boundaries or defer citation-bearing text until validated, and send final metadata for actual cited sources. It is acceptable to report grounded=false with citation warnings after streaming; do not silently invent/replace a citation. SSE client disconnect cancels generation and closes upstream response.

## Structured extraction (rag owns)

`ctfka.processing.extraction.KnowledgeExtractor(settings, store, provider).extract(article_id: str) -> dict` calls local chat with a Pydantic JSON schema. Fields: direction, keywords, vulnerability_type, root_cause, prerequisites, symbols, attack_steps, fix_steps, verification, tools, cards, evidence, confidence (nullable unreliable fields). Evidence must quote exact source and valid offsets. Preserve source_claimed versus ai_extracted versus human_verified; persistence is ai_extracted/unverified. Failed JSON/schema/evidence validation is recorded as failed and propagated honestly; rerun allowed. No commands or patches are executed.

## API and CLI (api owns)

Add GET /v1/models, GET /api/v1/models/status, POST /v1/chat/completions and POST /api/v1/chat/completions alias, POST /api/v1/rag alias optional, GET /api/v1/knowledge and /{id}, POST /api/v1/articles/{id}/extract. Extend search with mode=keyword|hybrid (existing default keyword stays compatible). Index rebuild supports semantic flag; defaults deterministic for stage1 compatibility. Expensive model generation/extraction/index writes use existing bearer auth and fail closed without token. Provider lifetime is per app, closed on shutdown.

Chat requests accept standard text messages/model/stream, temperature/top_p/max_tokens mapped explicitly to provider options. Validate roles and text; reject unsupported tool/image/n>1/response_format/etc arguments with explicit 422 rather than silently accepting. Return real HTTP errors for unavailable requested models or service before streaming; midstream errors are named SSE error frames and terminal [DONE], never fake successful completions.

Nonstream response uses ChatCompletion shape: id,object,created,model,choices[{index,message{role,content},finish_reason}],usage plus citations/retrieval metadata. SSE uses data: ChatCompletionChunk events (role/content, terminal finish_reason), optional final citations metadata, ends data: [DONE]\n\n. Validate chunks are JSON and usages are actual or absent, not made up. Auth for chat is explicit in docs/tests. Detect Request.is_disconnected and cancel upstream iterator in finally.

CLI adds `models status`, `index build --semantic/--no-semantic`, `search --mode hybrid`, `chat [QUESTION]` (interactive if omitted), `extract ARTICLE_ID`, `knowledge list/show`. Expose optional runtime limits as configs rather than arbitrary hard ceilings. README includes Ollama install/pull, disconnected-internet operation with local models, incremental indexing/rebuild after model changes, raw preservation on failures and API SSE examples. Version becomes 0.2.0.

## Acceptance

Run all stage 1 tests and new chunk/code/offset, incremental embedding reuse, model/dimension isolation, cosine/RRF, schema/evidence, prompt-injection isolation, no-model downgrade, normal/SSE contract, pre/midstream errors and cancellation tests. Fixtures may provide deterministic protocol responses, but record separately a real local installed model run: chunk+embed+hybrid search+grounded chat+structured extraction+SSE, then deny outbound internet for the test client while localhost remains reachable. No false claim of offline-model success if installation/download fails. Measure data/model sizes and timings. Root will handle runtime installation/live acceptance, final report, commit/push; agents do not commit.
