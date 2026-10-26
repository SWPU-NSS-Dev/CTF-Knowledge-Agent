from __future__ import annotations

import pytest

from ctfka.contracts import DiscoveredItem, FetchedDocument
from ctfka.models import Store


def test_expired_worker_cannot_mutate_reclaimed_job_or_article(tmp_path):
    store = Store(tmp_path)
    queued = store.enqueue_job("source", DiscoveredItem("https://example.test/item", "item"))
    old_owner = store.claim_job(lease_seconds=60)
    assert old_owner["id"] == queued["id"]
    with store._connection() as db:
        db.execute("UPDATE crawl_jobs SET lease_until=0 WHERE id=?", (queued["id"],))
    new_owner = store.claim_job(lease_seconds=60)
    assert new_owner["id"] == queued["id"]
    assert old_owner["lease_token"] != new_owner["lease_token"]

    old_token = old_owner["lease_token"]
    new_token = new_owner["lease_token"]
    assert store.heartbeat_job(queued["id"], 60, lease_token=old_token) is False
    assert store.release_job(queued["id"], lease_token=old_token) is False
    with pytest.raises(ValueError, match="job lease lost"):
        store.complete_job(queued["id"], {"owner": "stale"}, lease_token=old_token)
    with pytest.raises(ValueError, match="job lease lost"):
        store.fail_job(queued["id"], "stale failure", "network", lease_token=old_token)
    with pytest.raises(ValueError, match="job lease lost"):
        store.ingest(
            "source",
            FetchedDocument(
                "https://example.test/item", b"stale response", metadata={"identity": "item"}
            ),
            job_id=queued["id"],
            lease_token=old_token,
        )
    assert store.stats()["articles"] == 0
    assert not list(store.raw_dir.rglob("*"))

    assert store.heartbeat_job(queued["id"], 60, lease_token=new_token) is True
    article = store.ingest(
        "source",
        FetchedDocument(
            "https://example.test/item", b"current response", metadata={"identity": "item"}
        ),
        job_id=queued["id"],
        lease_token=new_token,
    )
    store.complete_job(queued["id"], article, lease_token=new_token)
    assert store.job_stats() == {"completed": 1}
    assert store.get_article(article["id"])["content"] == "current response"
