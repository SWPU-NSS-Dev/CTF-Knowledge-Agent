"""Keyword and local-vector retrieval with reciprocal-rank fusion."""

from __future__ import annotations

import asyncio
import re
from typing import Any

from ctfka.llm.base import LLMError

_TECHNICAL_TOKEN = re.compile(r"(?:CVE-\d{4}-\d+|[A-Za-z_][A-Za-z0-9_.$@:+/-]*)", re.IGNORECASE)
_CJK = re.compile(r"[\u3400-\u9fff]+")
_CJK_QUESTION_WORDS = ("如何", "怎么", "为什么", "什么", "是否", "哪些", "哪里", "请", "介绍", "解释", "分析")


class RetrievalService:
    def __init__(self, settings: Any, store: Any, provider: Any):
        self.settings = settings
        self.store = store
        self.provider = provider

    @staticmethod
    def _exact_query(query: str) -> tuple[str, str] | None:
        candidate = query.strip()
        if not candidate:
            return None
        if _TECHNICAL_TOKEN.fullmatch(candidate):
            return "technical", candidate
        if (
            len(candidate) <= 16
            and _CJK.fullmatch(candidate)
            and not any(word in candidate for word in _CJK_QUESTION_WORDS)
        ):
            return "phrase", candidate
        return None

    @classmethod
    def _has_exact_match(cls, item: dict[str, Any], exact_query: tuple[str, str] | None) -> bool:
        if exact_query is None:
            return False
        kind, term = exact_query
        searchable = f"{item.get('title', '')}\n{item.get('text', '')}"
        if kind == "phrase":
            return term in searchable
        boundary = r"A-Za-z0-9_.$@:+/-"
        pattern = rf"(?<![{boundary}]){re.escape(term)}(?![{boundary}])"
        return re.search(pattern, searchable, flags=re.IGNORECASE) is not None

    async def search(
        self,
        query: str,
        *,
        limit: int | None = None,
        source: str | None = None,
        semantic: bool = True,
    ) -> dict[str, Any]:
        config = self.settings.retrieval
        final_limit = limit or int(config.final_top_k)
        await asyncio.to_thread(self.store.sync_chunks, max_chars=int(self.settings.chunking.max_chars))
        keyword = await asyncio.to_thread(
            self.store.search_chunks, query, limit=int(config.keyword_top_k), source=source
        )
        warnings: list[str] = []
        vector: list[dict[str, Any]] = []
        model_key: str | None = None
        can_use_semantic = semantic and bool(config.semantic_enabled)
        if can_use_semantic:
            try:
                identity = await self.provider.embedding_identity()
                model_key = str(identity["model_key"])
                has_manifest = getattr(self.store, "has_embedding_manifest", None)
                if has_manifest is None or not await asyncio.to_thread(has_manifest, model_key):
                    warnings.append("no semantic index exists for the current embedding model; using keyword results")
                    return {
                        "items": keyword[:final_limit],
                        "mode": "keyword",
                        "warnings": warnings,
                        "embedding_model_key": model_key,
                    }
                query_vectors = await self.provider.embed([query])
                if len(query_vectors) != 1:
                    raise LLMError("provider returned an invalid query embedding count")
                vector = await asyncio.to_thread(
                    self.store.vector_search,
                    query_vectors[0],
                    model_key,
                    limit=int(config.vector_top_k),
                    source=source,
                )
            except (LLMError, KeyError, ValueError, RuntimeError) as exc:
                warnings.append(f"semantic retrieval unavailable; using keyword results: {exc}")
                model_key = None
                vector = []
        elif semantic and not bool(config.semantic_enabled):
            warnings.append("semantic retrieval is disabled by configuration")

        if not vector:
            if model_key and not warnings:
                warnings.append("no semantic chunks matched the available index; using keyword results")
            return {
                "items": keyword[:final_limit],
                "mode": "keyword",
                "warnings": warnings,
                "embedding_model_key": model_key,
            }

        # RRF scores rank positions, not relevance truth or answer confidence.
        rrf_k = int(config.rrf_k)
        fused: dict[str, dict[str, Any]] = {}
        for ranked in (keyword, vector):
            for rank, item in enumerate(ranked, start=1):
                chunk_id = str(item.get("chunk_id") or item.get("id"))
                if chunk_id not in fused:
                    fused[chunk_id] = {**item, "rrf_score": 0.0}
                fused[chunk_id]["rrf_score"] += 1.0 / (rrf_k + rank)
        exact_query = self._exact_query(query)
        boost = float(getattr(config, "exact_match_boost", 0.05))
        for item in fused.values():
            base_score = float(item["rrf_score"])
            matched_exactly = self._has_exact_match(item, exact_query)
            applied_boost = boost if matched_exactly else 0.0
            item["rrf_base_score"] = base_score
            item["exact_match_boost"] = applied_boost
            item["exact_match_boost_reason"] = (
                f"exact {exact_query[0]} query matched title/text" if matched_exactly else None
            )
            item["rrf_score"] = base_score + applied_boost
        items = sorted(fused.values(), key=lambda item: item["rrf_score"], reverse=True)
        return {
            "items": items[:final_limit],
            "mode": "hybrid",
            "warnings": warnings,
            "embedding_model_key": model_key,
        }
