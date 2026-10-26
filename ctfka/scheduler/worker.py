from __future__ import annotations

import asyncio
import logging
import sqlite3
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any

from ctfka.config import Settings, SourceConfig
from ctfka.contracts import DiscoveredItem, FetchedDocument
from ctfka.crawler import AdapterRegistry, FetchClient
from ctfka.crawler.adapters import SourceAdapter
from ctfka.models import Store

logger = logging.getLogger(__name__)


class LeaseLost(RuntimeError):
    pass


class _SourceGate:
    """Writer-fair async read/write gate for safe live source reconfiguration."""

    def __init__(self) -> None:
        self._condition = asyncio.Condition()
        self._readers = 0
        self._writer = False
        self._writers_waiting = 0

    @asynccontextmanager
    async def read(self):
        async with self._condition:
            while self._writer or self._writers_waiting:
                await self._condition.wait()
            self._readers += 1
        try:
            yield
        finally:
            async with self._condition:
                self._readers -= 1
                if not self._readers:
                    self._condition.notify_all()

    @asynccontextmanager
    async def write(self):
        async with self._condition:
            self._writers_waiting += 1
            try:
                while self._writer or self._readers:
                    await self._condition.wait()
                self._writer = True
            finally:
                self._writers_waiting -= 1
                if not self._writers_waiting:
                    self._condition.notify_all()
        try:
            yield
        finally:
            async with self._condition:
                self._writer = False
                self._condition.notify_all()


class CrawlService:
    """Coordinates complete discovery scans and durable leased jobs."""

    def __init__(self, settings: Settings, store: Store):
        self.settings = settings
        self.store = store
        self.registry = AdapterRegistry()
        self._stack = AsyncExitStack()
        self._sources: dict[str, tuple[FetchClient, SourceAdapter]] = {}
        self._source_lock = asyncio.Lock()
        self._source_semaphores: dict[str, asyncio.Semaphore] = {}
        self._discovery_locks: dict[str, asyncio.Lock] = {}
        self._source_gates: dict[str, _SourceGate] = {}
        self._wake_event = asyncio.Event()
        self._closed = False
        # Store.recover_jobs only releases leases that have actually expired.
        self.store.recover_jobs()

    def _config(self, source: str) -> SourceConfig:
        config = self.settings.sources.get(source)
        if config is None:
            raise KeyError(f"unknown source: {source}")
        if not config.enabled:
            raise ValueError(f"source {source!r} is disabled")
        return config

    def _gate(self, source: str) -> _SourceGate:
        if source not in self._source_gates:
            self._source_gates[source] = _SourceGate()
        return self._source_gates[source]

    async def _source(self, source: str) -> tuple[FetchClient, SourceAdapter]:
        if self._closed:
            raise RuntimeError("CrawlService is closed")
        cached = self._sources.get(source)
        if cached is not None:
            return cached
        async with self._source_lock:
            cached = self._sources.get(source)
            if cached is not None:
                return cached
            config = self._config(source)
            await asyncio.to_thread(
                self.store.register_source,
                source,
                config.adapter,
                config.model_dump(mode="json"),
            )
            client = await self._stack.enter_async_context(FetchClient(config))
            adapter = self.registry.create(config.adapter, config, client)
            self._sources[source] = (client, adapter)
            self._source_semaphores[source] = asyncio.Semaphore(config.concurrency)
            self._discovery_locks[source] = asyncio.Lock()
            return client, adapter

    async def refresh_source(self, source: str, config: SourceConfig | dict[str, Any]) -> None:
        """Replace a live adapter after an administrative source update."""
        parsed = config if isinstance(config, SourceConfig) else SourceConfig.model_validate(config)
        async with self._gate(source).write(), self._source_lock:
            old = self._sources.pop(source, None)
            if old is not None:
                client, adapter = old
                close = getattr(adapter, "close", None)
                if close is not None:
                    result = close()
                    if asyncio.iscoroutine(result):
                        await result
                await client.__aexit__(None, None, None)
            self.settings.sources[source] = parsed
            self._source_semaphores.pop(source, None)
            self._discovery_locks.pop(source, None)
            await asyncio.to_thread(
                self.store.register_source,
                source,
                parsed.adapter,
                parsed.model_dump(mode="json"),
            )

    @staticmethod
    def _committed_state(state: dict[str, Any]) -> dict[str, Any]:
        # A pending GitHub commit is deliberately kept separate from its cursor.
        return {key: value for key, value in state.items() if key != "pending"}

    async def _settle_pending(self, source: str) -> bool:
        """Promote a complete repository cursor only after every changed file lands."""
        state = await asyncio.to_thread(self.store.get_source_state, source)
        pending = state.get("pending") if isinstance(state, dict) else None
        if not isinstance(pending, dict):
            return False
        ids = pending.get("job_ids", [])
        if not ids:
            statuses: dict[str, str] = {}
        else:
            status_reader = getattr(self.store, "job_statuses", None)
            if status_reader is None:
                return False
            statuses = await asyncio.to_thread(status_reader, ids)
        # Missing rows, retries, failed jobs, and blocked jobs all hold the old cursor.
        if len(statuses) != len(ids) or any(value != "completed" for value in statuses.values()):
            return False
        candidate = pending.get("state")
        if not isinstance(candidate, dict):
            return False
        await asyncio.to_thread(self.store.set_source_state, source, candidate)
        return True

    async def discover(self, source: str) -> dict:
        result: dict[str, Any]
        async with self._gate(source).read():
            config = self._config(source)
            _, adapter = await self._source(source)
            lock = self._discovery_locks[source]
            async with lock:
                result = await self._discover_locked(source, config, adapter)
        if result.get("queued"):
            await self.wake()
        return result

    async def _discover_locked(
        self, source: str, config: SourceConfig, adapter: SourceAdapter
    ) -> dict:
        state = await asyncio.to_thread(self.store.get_source_state, source)
        if isinstance(state, dict) and isinstance(state.get("pending"), dict):
            # The durable queue already represents this uncommitted repository scan.
            # Avoid re-enqueuing successfully fetched files while one retry is pending.
            return {
                "source": source,
                "discovered": 0,
                "queued": 0,
                "pending": True,
                "cursor_advanced": await self._settle_pending(source),
            }

        cursor = self._committed_state(state) if isinstance(state, dict) else {}
        adapter.set_state(cursor)
        discovered = 0
        job_ids: list[str] = []
        async with self._source_semaphores[source]:
            async for item in adapter.discover(cursor or None):
                if not isinstance(item, DiscoveredItem):
                    raise TypeError(f"adapter {config.adapter!r} yielded a non-DiscoveredItem")
                if not item.url or not item.identity:
                    raise ValueError(f"adapter {config.adapter!r} yielded an empty URL or identity")
                record = await asyncio.to_thread(self.store.enqueue_job, source, item)
                job_id = str(record["id"])
                if job_id not in job_ids:
                    job_ids.append(job_id)
                discovered += 1

        # Archive only complete full-tree scans. The scanner has already yielded every
        # changed file, so each changed document is durable in the job queue.
        if bool(getattr(adapter, "complete_scan", False)):
            identities = list(getattr(adapter, "current_identities", []))
            await asyncio.to_thread(self.store.archive_missing, source, identities)
            candidate = adapter.state
            pending = {
                "state": candidate,
                "identities": identities,
                "job_ids": job_ids,
            }
            new_state = {**cursor, "pending": pending}
            await asyncio.to_thread(self.store.set_source_state, source, new_state)
            advanced = await self._settle_pending(source)
            return {
                "source": source,
                "discovered": discovered,
                "queued": discovered,
                "pending": not advanced,
                "cursor_advanced": advanced,
            }

        # Generic adapters may expose a cursor only once discovery exhausts. Persisting
        # it is safe here because every discovered item has been durably enqueued.
        candidate = adapter.state
        if candidate and candidate != cursor:
            await asyncio.to_thread(self.store.set_source_state, source, candidate)
        return {
            "source": source,
            "discovered": discovered,
            "queued": discovered,
            "cursor_advanced": False,
        }

    async def _fetch_and_ingest(
        self, job: dict, adapter: SourceAdapter, config: SourceConfig
    ) -> dict:
        item = DiscoveredItem(
            url=job["url"],
            identity=job["identity"],
            metadata=dict(job.get("metadata") or {}),
        )
        async with self._source_semaphores[job["source"]]:
            document: FetchedDocument = await adapter.fetch(item)
            # Store's identity key is taken from document metadata, not from the URL.
            # Preserve plugin identity (notably GitHub paths) through ingestion.
            document.metadata = {**item.metadata, **document.metadata, "identity": item.identity}
            parsed = adapter.normalize(adapter.parse(document))
            adapter.validate(parsed)
            result = await asyncio.to_thread(
                self.store.ingest,
                job["source"],
                document,
                parsed,
                job_id=job["id"],
                lease_token=job["lease_token"],
            )
            await asyncio.to_thread(
                self.store.complete_job,
                job["id"],
                result,
                lease_token=job["lease_token"],
            )
            return result

    async def _with_heartbeat(
        self, job: dict, adapter: SourceAdapter, config: SourceConfig
    ) -> dict:
        lease_seconds = 300.0
        heartbeat = getattr(self.store, "heartbeat_job", None)
        if heartbeat is None:
            return await self._fetch_and_ingest(job, adapter, config)
        operation = asyncio.create_task(self._fetch_and_ingest(job, adapter, config))

        async def pulse() -> None:
            while True:
                await asyncio.sleep(max(1.0, lease_seconds / 3))
                ok = await asyncio.to_thread(
                    heartbeat,
                    job["id"],
                    lease_seconds,
                    lease_token=job["lease_token"],
                )
                if not ok:
                    raise LeaseLost(f"lease for job {job['id']} was lost")

        heartbeat_task = asyncio.create_task(pulse())
        try:
            done, _ = await asyncio.wait(
                {operation, heartbeat_task}, return_when=asyncio.FIRST_COMPLETED
            )
            if heartbeat_task in done:
                error = heartbeat_task.exception()
                if error is not None:
                    operation.cancel()
                    await asyncio.gather(operation, return_exceptions=True)
                    raise error
            return await operation
        finally:
            heartbeat_task.cancel()
            await asyncio.gather(heartbeat_task, return_exceptions=True)
            if not operation.done():
                operation.cancel()
                await asyncio.gather(operation, return_exceptions=True)

    async def _release(self, job_id: str, lease_token: str) -> None:
        release = getattr(self.store, "release_job", None)
        if release is not None:
            try:
                await asyncio.to_thread(release, job_id, lease_token=lease_token)
            except sqlite3.Error:
                # The lease remains a recovery mechanism if SQLite is unavailable during
                # shutdown; do not replace the original cancellation with a DB error.
                logger.warning("could not release job lease during cancellation", exc_info=True)

    async def run(self, source: str | None = None, max_jobs: int | None = None) -> dict:
        if max_jobs is not None and max_jobs < 1:
            raise ValueError("max_jobs must be positive when provided")
        if source is not None:
            self._config(source)
        claimed = 0
        completed = 0
        failed = 0
        errors: list[dict[str, str]] = []
        claim_lock = asyncio.Lock()
        counts_lock = asyncio.Lock()

        async def next_job() -> dict | None:
            nonlocal claimed
            async with claim_lock:
                if max_jobs is not None and claimed >= max_jobs:
                    return None
                job = await asyncio.to_thread(self.store.claim_job, source)
                if job is not None:
                    claimed += 1
                return job

        async def worker() -> None:
            nonlocal completed, failed
            while True:
                job = await next_job()
                if job is None:
                    return
                try:
                    async with self._gate(job["source"]).read():
                        config = self._config(job["source"])
                        _, adapter = await self._source(job["source"])
                        await self._with_heartbeat(job, adapter, config)
                    async with counts_lock:
                        completed += 1
                except asyncio.CancelledError:
                    await self._release(job["id"], job["lease_token"])
                    raise
                except Exception as exc:  # noqa: BLE001 - plugin failures must be recorded durably
                    if isinstance(exc, LeaseLost) or (
                        isinstance(exc, ValueError) and str(exc) == "job lease lost"
                    ):
                        async with counts_lock:
                            errors.append(
                                {
                                    "job_id": job["id"],
                                    "error": "job lease lost; another worker owns the job",
                                }
                            )
                        continue
                    category = getattr(exc, "category", None)
                    if category is None:
                        category = (
                            "invalid" if isinstance(exc, (ValueError, TypeError)) else "network"
                        )
                    job_source_config = self.settings.sources.get(job["source"])
                    if job_source_config is None or not job_source_config.enabled:
                        category = "blocked"
                        exc = RuntimeError(
                            f"source {job['source']!r} is missing or disabled; job was not processed"
                        )
                    retry_after = getattr(exc, "retry_after", None)
                    try:
                        await asyncio.to_thread(
                            self.store.fail_job,
                            job["id"],
                            f"{type(exc).__name__}: {exc}",
                            category,
                            retry_after,
                            (
                                job_source_config.max_attempts
                                if job_source_config is not None
                                else int(job.get("max_attempts", 5))
                            ),
                            lease_token=job["lease_token"],
                        )
                    except sqlite3.Error as store_exc:
                        errors.append(
                            {
                                "job_id": job["id"],
                                "error": f"{type(store_exc).__name__}: {store_exc}",
                            }
                        )
                    async with counts_lock:
                        failed += 1
                        errors.append(
                            {"job_id": job["id"], "error": f"{type(exc).__name__}: {exc}"}
                        )

        await asyncio.gather(*(worker() for _ in range(self.settings.workers)))
        sources = [source] if source else list(self.settings.sources)
        advanced = []
        for name in sources:
            if name in self._sources and await self._settle_pending(name):
                advanced.append(name)
        return {
            "claimed": claimed,
            "completed": completed,
            "failed": failed,
            "errors": errors,
            "cursor_advanced": advanced,
            "remaining_retry_delay": await asyncio.to_thread(self.store.next_retry_delay),
        }

    async def crawl(self, source: str, max_jobs: int | None = None) -> dict:
        discovery = await self.discover(source)
        run = await self.run(source, max_jobs)
        return {"source": source, "discovery": discovery, "run": run}

    async def run_forever(self, source: str | None = None) -> None:
        """Poll durable work and due retries until the caller cancels the task."""
        if source is not None:
            self._config(source)
        while True:
            self._wake_event.clear()
            await self.run(source)
            delay = await asyncio.to_thread(self.store.next_retry_delay)
            timeout = 1.0 if delay is None else max(delay, 0.1)
            try:
                await asyncio.wait_for(self._wake_event.wait(), timeout=timeout)
            except TimeoutError:
                pass

    async def wake(self) -> None:
        """Wake a running supervisor after a job is enqueued outside discover()."""
        self._wake_event.set()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for _, adapter in self._sources.values():
            close = getattr(adapter, "close", None)
            if close is not None:
                result = close()
                if asyncio.iscoroutine(result):
                    await result
        await self._stack.aclose()
