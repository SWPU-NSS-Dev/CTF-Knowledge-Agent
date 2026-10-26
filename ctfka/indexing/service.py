from __future__ import annotations

import asyncio
from typing import Any


class IndexService:
    def __init__(self, settings, store, provider):
        self.settings = settings
        self.store = store
        self.provider = provider

    async def build(self, *, semantic: bool = True, article_id: str | None = None) -> dict[str, Any]:
        chunks_result = await asyncio.to_thread(
            self.store.sync_chunks,
            article_id=article_id,
            max_chars=self.settings.chunking.max_chars,
        )
        chunks = await asyncio.to_thread(
            self.store.list_chunks, article_id=article_id, current_only=True
        )
        result: dict[str, Any] = {
            "status": "keyword_ready",
            "chunks": chunks_result,
            "embedding_model_key": None,
            "embedded": 0,
            "reused": 0,
            "failed_batches": [],
            "semantic": False,
            "vector_backend": "sqlite-linear-cosine",
        }
        if not semantic or not self.settings.retrieval.semantic_enabled:
            return result
        try:
            identity = await self.provider.embedding_identity()
            model = identity["model"]
            digest = identity["digest"]
            model_key = identity["model_key"]
            result["embedding_model_key"] = model_key
            by_hash: dict[str, str] = {}
            for chunk in chunks:
                by_hash.setdefault(chunk["text_hash"], chunk["text"])
            cached = await asyncio.to_thread(self.store.get_embeddings, model_key, list(by_hash))
            result["reused"] = len(cached)
            missing = [(digest_key, text) for digest_key, text in by_hash.items() if digest_key not in cached]
            batch_size = self.settings.retrieval.embedding_batch_size
            for offset in range(0, len(missing), batch_size):
                batch = missing[offset : offset + batch_size]
                try:
                    vectors = await self.provider.embed([text for _key, text in batch])
                    if len(vectors) != len(batch):
                        raise ValueError("embedding provider returned an unexpected number of vectors")
                    dimension = self.settings.llm.embedding_dimensions or (len(vectors[0]) if vectors else 0)
                    items = [{"text_hash": key, "vector": vector} for (key, _text), vector in zip(batch, vectors)]
                    await asyncio.to_thread(
                        self.store.save_embeddings, model_key, model, digest, dimension, items
                    )
                    result["embedded"] += len(items)
                except Exception as exc:  # noqa: BLE001 - report failed batches for resumable indexing
                    result["failed_batches"].append({"offset": offset, "count": len(batch), "error": str(exc)})
            result["semantic"] = not result["failed_batches"]
            result["status"] = "complete" if result["semantic"] else "degraded"
        except Exception as exc:  # noqa: BLE001 - preserve keyword indexing through provider outages
            result["status"] = "degraded"
            result["error"] = str(exc)
        return result
