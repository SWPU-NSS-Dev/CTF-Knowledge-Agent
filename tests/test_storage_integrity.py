from __future__ import annotations

import zipfile

import pytest

from ctfka.contracts import DiscoveredItem, FetchedDocument, ParsedDocument
from ctfka.models import Store


def test_changed_raw_bytes_create_distinct_article_version(tmp_path):
    store = Store(tmp_path)
    first = store.ingest(
        "s",
        FetchedDocument("https://e.test", b"raw one"),
        ParsedDocument("Title", "same normalized text"),
    )
    second = store.ingest(
        "s",
        FetchedDocument("https://e.test", b"raw two"),
        ParsedDocument("Title", "same normalized text"),
    )
    assert first["version"] == 1 and second["version"] == 2
    assert store.get_article(first["id"])["versions"][0]["raw_sha256"] == first["raw_sha256"]


def test_large_scan_identities_and_source_state_are_durable(tmp_path):
    store = Store(tmp_path)
    store.register_source("s", "adapter", {"key": "value"})
    store.set_source_state("s", {"cursor": "rev-1"})
    store.ingest(
        "s", FetchedDocument("https://e.test/keep", b"keep", metadata={"identity": "keep"})
    )
    identities = ["missing-" + str(i) for i in range(1500)] + ["keep"]
    assert store.archive_missing("s", identities) == 0
    reopened = Store(tmp_path)
    assert reopened.get_source_state("s") == {"cursor": "rev-1"}
    assert reopened.list_sources()[0]["config"] == {"key": "value"}


def test_restore_points_raw_paths_into_restored_store(tmp_path):
    source = Store(tmp_path / "source")
    stored = source.ingest("s", FetchedDocument("https://e.test", b"raw preserved"))
    archive = source.backup(tmp_path / "backup.zip")
    restored = Store(tmp_path / "restored")
    restored.restore(archive)
    article = restored.get_article(stored["id"])
    assert article["raw_path"].startswith(str(restored.raw_dir))
    with open(article["raw_path"], "rb") as saved:
        assert saved.read() == b"raw preserved"


def test_restore_rejects_unlisted_archive_entries(tmp_path):
    source = Store(tmp_path / "source")
    source.ingest("s", FetchedDocument("https://e.test", b"x"))
    archive = source.backup(tmp_path / "backup.zip")
    with zipfile.ZipFile(archive, "a") as zf:
        zf.writestr("raw/evil", b"extra")
    with pytest.raises(ValueError, match="manifest"):
        Store(tmp_path / "restore").restore(archive)


def test_job_completion_is_idempotent(tmp_path):
    store = Store(tmp_path)
    queued = store.enqueue_job("s", DiscoveredItem("https://e.test", "e"))
    assert store.list_jobs()[0]["status"] == "pending"
    store.claim_job()
    store.complete_job(queued["id"], {"ok": True})
    store.complete_job(queued["id"], {"ok": True})
    assert store.job_stats() == {"completed": 1}


def test_v1_database_migrates_to_simhash_v2(tmp_path):
    store = Store(tmp_path)
    store.ingest("s", FetchedDocument("https://e.test/migrate", b"migration evidence"))
    with store._connection() as db:
        db.execute("DROP INDEX articles_simhash_idx")
        db.execute("ALTER TABLE articles DROP COLUMN simhash")
        db.execute("DELETE FROM schema_migrations WHERE version=2")
    migrated = Store(tmp_path)
    with migrated._connection() as db:
        version = db.execute("SELECT max(version) FROM schema_migrations").fetchone()[0]
        signature = db.execute("SELECT simhash FROM articles").fetchone()[0]
    assert version == 2
    assert signature != 0
