from __future__ import annotations

import argparse
import json
import platform
import sqlite3
import statistics
import sys
import tempfile
import time
from pathlib import Path

from ctfka.contracts import FetchedDocument, ParsedDocument
from ctfka.models import Store

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_PATH = ROOT / "fixtures" / "retrieval_eval.json"


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int((len(ordered) - 1) * fraction + 0.5)))
    return ordered[index]


def generate_bulk_document(index: int) -> tuple[str, str]:
    # Every generated record has distinct technical detail and stable, reproducible text.
    families = [
        ("Web request validation", "HTTP parser", "canonical path checks"),
        ("Linux process memory", "mmap regions", "page permission validation"),
        ("Cryptographic protocol", "nonce handling", "authenticated message parsing"),
        ("Database query safety", "parameter binding", "transaction boundaries"),
        ("Firmware analysis", "UART boot logs", "image integrity checks"),
        ("Network service hardening", "socket lifecycle", "bounded request framing"),
        ("Binary analysis workflow", "symbol tables", "reproducible debugger notes"),
        ("Patch verification", "regression cases", "negative input coverage"),
        ("Container isolation", "capability sets", "read-only mount checks"),
        ("Incident response", "timeline evidence", "artifact hash recording"),
    ]
    family, component, check = families[index % len(families)]
    cycle = index // len(families)
    title = f"{family}: case {cycle:05d} component {component}"
    content = (
        f"# {title}\n\n"
        f"This independent CTF engineering note studies {component} in {family.lower()}.\n"
        f"Case identifier: eval-{index:06d}. Review {check}, record the observed behavior, "
        f"and verify the fix with input set {cycle:05d}.\n\n"
        f"The analysis keeps evidence, assumptions, and validation steps explicit."
    )
    return title, content


def run(document_count: int, limit: int) -> dict:
    fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    reference_docs = fixture["documents"]
    if document_count < len(reference_docs):
        raise ValueError(
            f"--documents must be at least {len(reference_docs)} for this evaluation set"
        )
    with tempfile.TemporaryDirectory(prefix="ctfka-retrieval-eval-") as temporary:
        store = Store(Path(temporary) / "data")
        started = time.perf_counter()
        indexed: dict[str, str] = {}
        for item in reference_docs:
            result = store.ingest(
                "eval-fixture",
                FetchedDocument(
                    f"https://fixture.invalid/{item['id']}.md",
                    item["content"].encode("utf-8"),
                    "text/markdown",
                    {"identity": item["id"]},
                ),
                ParsedDocument(item["title"], item["content"], {"parser_version": "eval-1"}),
            )
            indexed[item["id"]] = result["id"]
        for index in range(document_count - len(reference_docs)):
            title, content = generate_bulk_document(index)
            store.ingest(
                "eval-generated",
                FetchedDocument(
                    f"https://fixture.invalid/generated/{index:06d}.md",
                    content.encode("utf-8"),
                    "text/markdown",
                    {"identity": f"generated-{index:06d}"},
                ),
                ParsedDocument(title, content, {"parser_version": "eval-1"}),
            )
        ingestion_seconds = time.perf_counter() - started
        query_latencies: list[float] = []
        hit_count = 0
        recalls: list[float] = []
        query_results = []
        for case in fixture["queries"]:
            begin = time.perf_counter()
            found = store.search(case["query"], limit=limit)
            query_latencies.append((time.perf_counter() - begin) * 1000)
            found_ids = {row["id"] for row in found}
            relevant = {indexed[item_id] for item_id in case["relevant"]}
            hits = len(found_ids & relevant)
            hit_count += int(hits > 0)
            recalls.append(hits / len(relevant) if relevant else 1.0)
            query_results.append(
                {
                    "query": case["query"],
                    "hit": hits > 0,
                    "relevant_found": hits,
                    "relevant_total": len(relevant),
                    "result_ids": [row["identity"] for row in found],
                }
            )
        return {
            "benchmark": "synthetic_fixture_offline_retrieval",
            "notice": "Synthetic fixture benchmark; ingestion rate excludes network discovery and fetch throughput.",
            "environment": {
                "python": sys.version.split()[0],
                "platform": platform.platform(),
                "sqlite": sqlite3.sqlite_version,
            },
            "documents": document_count,
            "ingestion_seconds": round(ingestion_seconds, 6),
            "ingestion_rate_documents_per_second": round(document_count / ingestion_seconds, 3)
            if ingestion_seconds
            else None,
            "query_count": len(query_latencies),
            "query_latency_ms": {
                "p50": round(statistics.median(query_latencies), 3),
                "p95": round(percentile(query_latencies, 0.95), 3),
            },
            f"hit@{limit}": round(hit_count / len(query_results), 4) if query_results else 0.0,
            f"recall@{limit}": round(statistics.mean(recalls), 4) if recalls else 0.0,
            "queries": query_results,
        }


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a deterministic offline CTF search benchmark")
    parser.add_argument(
        "--documents",
        type=int,
        default=300,
        help="total synthetic documents (minimum 10; default 300)",
    )
    parser.add_argument(
        "--limit", type=int, default=5, help="search result cutoff K for Hit@K and Recall@K"
    )
    args = parser.parse_args()
    if args.documents < 10 or args.limit < 1:
        parser.error("--documents must be >= 10 and --limit must be >= 1")
    print(json.dumps(run(args.documents, args.limit), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
