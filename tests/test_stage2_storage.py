from __future__ import annotations

import pytest

from ctfka.config import LLMConfig, RetrievalConfig, Settings
from ctfka.contracts import FetchedDocument
from ctfka.models import Store
from ctfka.processing.chunking import chunk_document


def test_chunk_offsets_heading_hierarchy_and_source_coverage():
    source = "# H1\n\n## H2\n\n### H3\n\na\n\n## H2b\n\nb\n"
    chunks = chunk_document(source, article_id="a", version=1, max_chars=5)
    assert "".join(chunk["text"] for chunk in chunks) == source
    assert all(source[item["start"] : item["end"]] == item["text"] for item in chunks)
    heading_paths = [item["heading_path"] for item in chunks]
    assert heading_paths[0] == ["H1"]
    assert ["H1", "H2"] in heading_paths
    assert ["H1", "H2", "H3"] in heading_paths
    assert ["H1", "H2b"] in heading_paths
    assert all(item["id"] for item in chunks)


def test_oversized_fenced_and_indented_code_stays_whole():
    source = "intro\n\n```python\nx = 1\ny = 2\n```\n\n    one\n    two\n\nend\n"
    chunks = chunk_document(source, article_id="a", version=1, max_chars=4)
    assert "".join(chunk["text"] for chunk in chunks) == source
    assert any("```python" in chunk["text"] and "y = 2" in chunk["text"] for chunk in chunks)
    assert any("    one\n    two" in chunk["text"] for chunk in chunks)


def test_index_storage_incremental_vectors_and_extractions(tmp_path):
    store = Store(tmp_path)
    article = store.ingest("fixture", FetchedDocument("https://e.test/a", b"# A\n\nCVE-2024-1234"))
    first = store.sync_chunks(max_chars=100)
    assert first["created"] == 1
    assert store.sync_chunks(max_chars=100)["skipped"] == 1
    chunk = store.list_chunks()[0]
    store.save_embeddings("mk", "model", "digest", 2, [{"text_hash": chunk["text_hash"], "vector": [1, 0]}])
    assert store.get_embeddings("mk", [chunk["text_hash"]])[chunk["text_hash"]] == [1.0, 0.0]
    assert store.vector_search([1, 0], "mk")[0]["id"] == chunk["id"]
    with pytest.raises(ValueError, match="manifest"):
        store.save_embeddings("mk", "model", "digest", 3, [{"text_hash": "bad", "vector": [1, 0, 0]}])
    extraction = store.save_extraction(
        article["id"],
        1,
        "chat",
        "digest",
        {
            "evidence": [{"quote": "CVE-2024-1234", "start": 5, "end": 18}],
            "cards": [{"title": "CVE", "content": "fact", "evidence_indices": [0]}],
        },
    )
    assert store.list_extractions(article["id"])[0]["id"] == extraction["id"]
    card = store.list_knowledge()[0]
    assert card["verification_status"] == "unverified"
    assert card["evidence"] == [{"quote": "CVE-2024-1234", "start": 5, "end": 18}]
    assert card["model"] == "chat" and card["model_digest"] == "digest"
    assert card["source_url"] == "https://e.test/a" and card["raw_path"]


def test_chunk_search_and_version_filtering(tmp_path):
    store = Store(tmp_path)
    first = store.ingest("one", FetchedDocument("https://e.test/a", b"old __free_hook evidence"))
    store.sync_chunks()
    second = store.ingest("one", FetchedDocument("https://e.test/a", b"new CVE-2024-9876 evidence"))
    store.sync_chunks()
    assert first["id"] == second["id"]
    assert len(store.list_chunks(current_only=False)) == 2
    assert len(store.list_chunks(current_only=True)) == 1
    hit = store.search_chunks("CVE-2024-9876")
    assert len(hit) == 1 and hit[0]["version"] == 2
    assert store.search_chunks("__free_hook") == []


def test_fts_chinese_bigram_recall(tmp_path):
    store = Store(tmp_path)
    store.ingest("fixture", FetchedDocument("https://e.test/a", "利用栈溢出漏洞完成利用链".encode()))
    store.sync_chunks()
    hits = store.search_chunks("漏洞类型是什么栈溢出")
    assert hits and "栈溢出漏洞" in hits[0]["text"]


def test_v1_stage2_fts_migrates_to_symbol_column(tmp_path):
    store = Store(tmp_path)
    store.ingest("fixture", FetchedDocument("https://e.test/a", "栈溢出漏洞".encode()))
    store.sync_chunks()
    with store._connection() as db:
        db.execute("DROP TABLE chunk_fts")
        db.execute("CREATE VIRTUAL TABLE chunk_fts USING fts5(chunk_id UNINDEXED,text)")
        chunk = db.execute("SELECT id,text FROM chunks").fetchone()
        db.execute("INSERT INTO chunk_fts VALUES(?,?)", (chunk["id"], chunk["text"]))
        db.execute("DELETE FROM stage2_schema_migrations WHERE version=2")
    migrated = Store(tmp_path)
    assert migrated.search_chunks("漏洞类型栈溢出")
    assert migrated.stats()["stage2_schema_version"] == 2


def test_config_defaults_and_reranker_rejection():
    settings = Settings()
    assert settings.llm == LLMConfig()
    assert settings.llm.think is True
    assert settings.llm.structured_think is False
    assert LLMConfig(think=False).think is False
    assert LLMConfig(structured_think=True).structured_think is True
    assert settings.retrieval == RetrievalConfig()
    with pytest.raises(ValueError, match="not implemented"):
        RetrievalConfig(use_reranker=True)
