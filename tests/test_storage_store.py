from __future__ import annotations

import zipfile

import pytest

from ctfka.contracts import DiscoveredItem, FetchedDocument
from ctfka.ingestion import parse_document
from ctfka.models import Store


def test_ingest_versions_search_and_raw_immutability(tmp_path):
    store = Store(tmp_path / "dbdata")
    store.register_source("fixture", "local")
    first_raw = "# Hook\n\n`__free_hook` GOT\n\nChinese 栈溢出".encode()
    first = store.ingest("fixture", FetchedDocument("https://example.test/a", first_raw))
    assert first["version"] == 1
    raw_path = first["raw_path"]
    with open(raw_path, "rb") as saved:
        assert saved.read() == first_raw
    repeated = store.ingest("fixture", FetchedDocument("https://example.test/a", first_raw))
    assert repeated["duplicate"] is True and repeated["version"] == 1
    assert store.search("__free_hook")
    assert store.search("栈溢出")
    second = store.ingest(
        "fixture",
        FetchedDocument("https://example.test/a", b"# Hook\n\n`__free_hook` PLT updated"),
    )
    assert second["version"] == 2
    article = store.get_article(first["id"])
    assert len(article["versions"]) == 2
    with open(raw_path, "rb") as saved:
        assert saved.read() == first_raw


def test_jobs_idempotency_lease_retry_and_recovery(tmp_path):
    store = Store(tmp_path)
    item = DiscoveredItem("https://example.test/x", "x")
    a = store.enqueue_job("s", item)
    assert store.enqueue_job("s", item)["id"] == a["id"]
    claimed = store.claim_job(lease_seconds=0.01)
    assert claimed["id"] == a["id"]
    assert store.heartbeat_job(a["id"], 60)
    assert store.release_job(a["id"])
    claimed = store.claim_job(lease_seconds=0.01)
    store.fail_job(a["id"], "temporary", "timeout", retry_after=0)
    assert store.job_stats()["retry_waiting"] == 1
    claimed = store.claim_job()
    assert claimed["attempts"] == 3
    assert store.job_statuses([a["id"]])[a["id"]] == "running"
    # Simulate an expired worker lease and verify restart recovery.
    with store._connect() as db:
        db.execute("UPDATE crawl_jobs SET lease_until=0 WHERE id=?", (a["id"],))
    assert store.recover_jobs() == 1


def test_html_parser_and_archive_missing(tmp_path):
    parsed = parse_document(
        FetchedDocument(
            "https://example.test",
            b"<html><head><title>CTF</title></head><body><nav>noise</nav><article><h1>Title</h1><pre><code>R_X86_64_JUMP_SLOT</code></pre></article></body></html>",
            "text/html",
        )
    )
    assert parsed.title == "Title"
    assert "noise" not in parsed.content
    assert "R_X86_64_JUMP_SLOT" in parsed.content
    store = Store(tmp_path)
    store.ingest("src", FetchedDocument("https://e.test/1", b"one", metadata={"identity": "1"}))
    store.ingest("src", FetchedDocument("https://e.test/2", b"two", metadata={"identity": "2"}))
    assert store.archive_missing("src", ["1"]) == 1
    assert len(store.search("one")) == 1
    assert len(store.search("two")) == 0


def test_backup_manifest_integrity_and_restore(tmp_path):
    store = Store(tmp_path / "origin")
    store.ingest("src", FetchedDocument("https://e.test/a", b"snapshot"))
    archive = store.backup(tmp_path / "backup.zip")
    target = Store(tmp_path / "target")
    assert target.restore(archive)["articles"] == 1
    assert target.search("snapshot")
    with zipfile.ZipFile(archive, "a") as z:
        z.writestr("raw/bad", b"tampered")
    with pytest.raises(ValueError, match="manifest"):
        target.restore(archive)
