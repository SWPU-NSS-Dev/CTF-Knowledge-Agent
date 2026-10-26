from __future__ import annotations

import argparse
import asyncio
import ipaddress
import json
import math
import platform
import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from statistics import median
from typing import Any
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ctfka.config import Settings
from ctfka.contracts import FetchedDocument, ParsedDocument
from ctfka.indexing import IndexService
from ctfka.llm.ollama import OllamaProvider
from ctfka.models import Store
from ctfka.retrieval.service import RetrievalService


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Evaluate keyword and local Ollama hybrid retrieval on the fixed synthetic fixture."
    )
    parser.add_argument("--config", type=Path, help="optional CTFKA YAML configuration")
    parser.add_argument("--documents", type=int, default=30, help="maximum fixture documents to index (default: 30)")
    parser.add_argument("--top-k", type=int, default=5, help="retrieval cutoff for metrics (default: 5)")
    parser.add_argument(
        "--fixture",
        type=Path,
        default=ROOT / "fixtures" / "retrieval_eval.json",
        help="evaluation fixture JSON",
    )
    parsed = parser.parse_args()
    if parsed.documents < 1:
        parser.error("--documents must be positive")
    if parsed.top_k < 1:
        parser.error("--top-k must be positive")
    return parsed


def _percentile95(values: list[float]) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)]


def _metrics(results: list[tuple[dict[str, Any], list[str]]], timings: list[float], top_k: int) -> dict[str, Any]:
    hits = relevant_found = relevant_total = top1_hits = 0
    reciprocal_rank_total = 0.0
    per_query = []
    for query_result, relevant in results:
        expected_ids = query_result["expected_article_ids"]
        article_to_fixture = {article_id: fixture_id for fixture_id, article_id in expected_ids.items()}
        returned_fixture_ids: list[str] = []
        seen: set[str] = set()
        for item in query_result["items"][:top_k]:
            fixture_id = article_to_fixture.get(str(item["article_id"]))
            if fixture_id is not None and fixture_id not in seen:
                returned_fixture_ids.append(fixture_id)
                seen.add(fixture_id)
        returned = [expected_ids[fixture_id] for fixture_id in returned_fixture_ids]
        relevant_ids = {expected_ids[key] for key in relevant if key in expected_ids}
        overlap = relevant_ids.intersection(returned)
        hits += bool(overlap)
        relevant_found += len(overlap)
        relevant_total += len(relevant_ids)
        if returned and returned[0] in relevant_ids:
            top1_hits += 1
        first_relevant_rank = next(
            (rank for rank, article_id in enumerate(returned, start=1) if article_id in relevant_ids),
            None,
        )
        if first_relevant_rank is not None:
            reciprocal_rank_total += 1 / first_relevant_rank
        per_query.append(
            {
                "query": query_result["query"],
                "relevant_fixture_ids": relevant,
                "returned_fixture_ids": returned_fixture_ids,
                "hit": bool(overlap),
                "recall": len(overlap) / len(relevant_ids) if relevant_ids else 0.0,
                "reciprocal_rank": 1 / first_relevant_rank if first_relevant_rank else 0.0,
            }
        )
    return {
        "hit_at_k": hits / len(results) if results else 0.0,
        "recall_at_k": relevant_found / relevant_total if relevant_total else 0.0,
        "top1_accuracy": top1_hits / len(results) if results else 0.0,
        "mrr_at_k": reciprocal_rank_total / len(results) if results else 0.0,
        "p50_ms": median(timings) if timings else 0.0,
        "p95_ms": _percentile95(timings) if timings else 0.0,
        "queries": per_query,
    }


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    fixture = json.loads(args.fixture.read_text(encoding="utf-8"))
    documents = fixture.get("documents")
    queries = fixture.get("queries")
    if not isinstance(documents, list) or not documents or not isinstance(queries, list) or not queries:
        raise ValueError("evaluation fixture must contain non-empty documents and queries arrays")
    selected = documents[: args.documents]
    settings = Settings.load(args.config)
    endpoint = urlsplit(settings.llm.base_url)
    host = endpoint.hostname or ""
    if host.lower() != "localhost":
        try:
            if not ipaddress.ip_address(host).is_loopback:
                raise RuntimeError("evaluation requires a loopback Ollama base_url")
        except ValueError as exc:
            raise RuntimeError("evaluation requires a loopback Ollama base_url") from exc
    if not settings.retrieval.semantic_enabled:
        settings = Settings.model_validate(
            {
                **settings.model_dump(),
                "retrieval": {**settings.retrieval.model_dump(), "semantic_enabled": True},
            }
        )

    with tempfile.TemporaryDirectory(prefix="ctfka-hybrid-eval-") as temporary:
        store = Store(temporary)
        store.register_source("synthetic-eval", "fixture")
        fixture_to_article: dict[str, str] = {}
        for document in selected:
            fixture_id = str(document["id"])
            ingested = store.ingest(
                "synthetic-eval",
                FetchedDocument(
                    url=f"https://fixture.invalid/{fixture_id}",
                    raw=str(document["content"]).encode("utf-8"),
                    metadata={"identity": fixture_id},
                ),
                ParsedDocument(str(document["title"]), str(document["content"])),
            )
            fixture_to_article[fixture_id] = str(ingested["id"])
        eligible_queries = [
            entry
            for entry in queries
            if any(str(item) in fixture_to_article for item in entry.get("relevant", []))
        ]
        if not eligible_queries:
            raise ValueError("selected documents do not contain any query's relevant fixture item")

        async with OllamaProvider(settings.llm) as provider:
            status = await provider.status()
            if not status.get("reachable"):
                raise RuntimeError(f"Ollama is unavailable at {settings.llm.base_url}: {status.get('error')}")
            if not status.get("embedding_model_available"):
                raise RuntimeError(
                    f"configured embedding model is not installed: {settings.llm.embedding_model}; no model was pulled"
                )
            identity = await provider.embedding_identity()
            indexing = IndexService(settings, store, provider)
            cold_start = time.perf_counter()
            cold = await indexing.build(semantic=True)
            cold_seconds = time.perf_counter() - cold_start
            if cold.get("failed_batches"):
                raise RuntimeError(f"embedding batches failed: {cold['failed_batches']}")
            if cold.get("status") != "complete" or not cold.get("semantic"):
                raise RuntimeError(f"semantic indexing did not complete: {cold}")

            reuse_start = time.perf_counter()
            reuse = await indexing.build(semantic=True)
            reuse_seconds = time.perf_counter() - reuse_start
            if reuse.get("failed_batches") or reuse.get("status") != "complete":
                raise RuntimeError(f"incremental indexing did not complete: {reuse}")
            if reuse.get("embedded", 0) != 0:
                raise RuntimeError(f"incremental indexing unexpectedly embedded chunks: {reuse}")

            retrieval = RetrievalService(settings, store, provider)
            mode_results: dict[str, list[tuple[dict[str, Any], list[str]]]] = {"keyword": [], "hybrid": []}
            mode_timings: dict[str, list[float]] = {"keyword": [], "hybrid": []}
            for entry in eligible_queries:
                query = str(entry["query"])
                relevant = [str(item) for item in entry.get("relevant", []) if item in fixture_to_article]
                for mode, semantic in (("keyword", False), ("hybrid", True)):
                    started = time.perf_counter()
                    result = await retrieval.search(query, limit=args.top_k, semantic=semantic)
                    mode_timings[mode].append((time.perf_counter() - started) * 1000)
                    expected_map = {key: fixture_to_article[key] for key in fixture_to_article}
                    mode_results[mode].append(
                        ({"query": query, "items": result["items"], "expected_article_ids": expected_map}, relevant)
                    )
                    if mode == "hybrid" and result.get("mode") != "hybrid":
                        raise RuntimeError(
                            f"hybrid retrieval degraded to {result.get('mode')} for query {query!r}: {result.get('warnings')}"
                        )

            db_path = Path(temporary) / "db" / "ctfka.sqlite3"
            db_bytes = db_path.stat().st_size if db_path.exists() else 0
            model_entry = next(
                (item for item in await provider.list_models() if item.get("name") == identity.get("model")),
                {},
            )
            return {
                "corpus_kind": "synthetic_fixture",
                "dataset_description": fixture.get("description"),
                "fixture_path": str(args.fixture),
                "requested_document_limit": args.documents,
                "fixture_available_documents": len(documents),
                "corpus_count": len(selected),
                "query_count": len(eligible_queries),
                "fixture_query_count": len(queries),
                "top_k": args.top_k,
                "model": {"name": identity.get("model"), "digest": identity.get("digest"), "size_bytes": model_entry.get("size")},
                "environment": {
                    "python": platform.python_version(),
                    "platform": platform.platform(),
                    "sqlite": sqlite3.sqlite_version,
                    "ollama": status.get("version"),
                    "ollama_base_url": settings.llm.base_url,
                    "network_crawling": False,
                    "model_pull": False,
                },
                "indexing": {
                    "cold_seconds": cold_seconds,
                    "incremental_reuse_seconds": reuse_seconds,
                    "cold_chunks_embedded": cold.get("embedded"),
                    "cold_chunks_reused": cold.get("reused"),
                    "incremental_chunks_embedded": reuse.get("embedded"),
                    "incremental_chunks_reused": reuse.get("reused"),
                    "sqlite_bytes": db_bytes,
                    "vector_backend": cold.get("vector_backend", "sqlite-linear-cosine"),
                },
                "metrics": {
                    mode: _metrics(mode_results[mode], mode_timings[mode], args.top_k)
                    for mode in ("keyword", "hybrid")
                },
                "interpretation": "Results measure this fixed synthetic fixture only; they do not represent real-world CTF retrieval quality.",
            }


def main() -> int:
    args = _args()
    try:
        result = asyncio.run(_run(args))
    except Exception as exc:  # noqa: BLE001 - CLI must turn provider and batch failures into clear nonzero exits
        print(f"hybrid evaluation failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
