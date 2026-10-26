from __future__ import annotations

import pytest

from ctfka.config import AgentConfig, Settings
from ctfka.contracts import FetchedDocument, ParsedDocument
from ctfka.knowledge.fixtures import seed_awdp_fixture
from ctfka.knowledge.storage import RevisionConflict
from ctfka.models.store import Store


def test_agent_config_optional_positive_limits():
    assert Settings().agent == AgentConfig()
    assert AgentConfig(max_tool_calls=None, max_context_tokens=None, timeout_seconds=None, max_relation_depth=None).max_tool_calls is None
    with pytest.raises(ValueError):
        AgentConfig(max_tool_calls=0)


def test_fixture_is_exact_and_idempotent(tmp_path):
    store = Store(tmp_path)
    first = seed_awdp_fixture(store)
    second = seed_awdp_fixture(store)
    assert first["article_id"] == second["article_id"]
    assert first["entities"] == second["entities"]
    assert len(store.list_entities(limit=100)) == 13
    assert len(store.list_relations(limit=100)) == 18
    challenge = store.get_entity(first["entities"]["challenge"])
    evidence = challenge["evidence"][0]
    historical = store.get_article_version(evidence["article_id"], evidence["version"])
    assert historical["content"][evidence["start"]:evidence["end"]] == evidence["quote"]
    assert evidence["source_status"] == "source_claimed"
    assert len(store.find_attack_fix(entity_id=challenge["id"])["comparisons"]) == 2
    assert store.list_entities(filters={"year": "2025"}, limit=100)
    assert evidence["title"] == historical["title"]
    graph = store.related(challenge["id"], depth=None, limit=None)
    relation_ids = [item["id"] for item in graph["relations"]]
    assert len(relation_ids) == len(set(relation_ids))
    assert len(graph["entities"]) == 13


def test_review_revision_and_stale_endpoint_filter(tmp_path):
    store = Store(tmp_path)
    seeded = seed_awdp_fixture(store)
    relation = next(r for r in store.list_relations(entity_id=seeded["entities"]["attack"]) if r["kind"] == "attack_fix")
    reviewed = store.review_record("relation", relation["id"], "human_verified", note="Checked against fixture evidence", reviewer="tester", expected_revision=relation["revision"])
    assert reviewed["decision"] == "human_verified"
    assert store.get_relation(relation["id"])["status"] == "human_verified"
    endpoint = store.get_entity(relation["source_id"])
    store.update_entity(endpoint["id"], {"title": endpoint["title"] + " revised"}, expected_revision=endpoint["revision"])
    assert store.get_relation(relation["id"])["status"] == "candidate"
    assert relation["id"] not in {r["id"] for r in store.list_relations(status="human_verified", limit=100)}
    assert store.knowledge_stats()["relation_status"].get("candidate", 0) >= 1
    with pytest.raises(RevisionConflict):
        store.update_entity(endpoint["id"], {"title": "stale"}, expected_revision=endpoint["revision"])


def test_invalid_evidence_and_backup_restore_graph(tmp_path):
    store = Store(tmp_path / "source")
    seeded = seed_awdp_fixture(store)
    challenge = store.get_entity(seeded["entities"]["challenge"])
    ev = challenge["evidence"][0]
    bad = dict(ev, quote="wrong")
    with pytest.raises(ValueError, match="exactly match"):
        store.create_entity({"kind": "knowledge", "title": "Bad claim", "evidence": [bad]})
    archive = store.backup(tmp_path / "stage3.zip")
    restored = Store(tmp_path / "restored")
    result = restored.restore(archive)
    assert result["articles"] == 1
    assert restored.get_entity(challenge["id"])["title"] == challenge["title"]
    assert len(restored.list_relations(limit=100)) == len(seeded["relations"])
    assert restored.stats()["stage3_schema_version"] == 1


def test_interrupted_stage3_schema_migration_resumes(tmp_path):
    import sqlite3

    data = tmp_path / "partial"
    db_path = data / "db" / "ctfka.sqlite3"
    db_path.parent.mkdir(parents=True)
    with sqlite3.connect(db_path) as db:
        db.execute("CREATE TABLE stage3_schema_migrations(version INTEGER PRIMARY KEY,applied_at REAL NOT NULL)")
        db.execute("CREATE TABLE entities(id TEXT PRIMARY KEY,kind TEXT NOT NULL,title TEXT NOT NULL,description TEXT NOT NULL,attributes_json TEXT NOT NULL,evidence_json TEXT NOT NULL,provenance TEXT NOT NULL,verification_status TEXT NOT NULL,revision INTEGER NOT NULL,created_at REAL NOT NULL,updated_at REAL NOT NULL)")
        db.execute("CREATE INDEX entities_kind_idx ON entities(kind,title)")
        db.execute("INSERT INTO entities VALUES('preserved','knowledge','kept','partial migration row','{}','[]','human_authored','unverified',1,1,1)")
    store = Store(data)
    assert store.get_entity("preserved")["title"] == "kept"
    assert store.create_entity({"kind": "knowledge", "title": "new"})["title"] == "new"


def test_pending_review_pagination_is_global_and_knowledge_revision_persists(tmp_path):
    store = Store(tmp_path)
    seed_awdp_fixture(store)
    body = "A source backed knowledge fact."
    article = store.ingest("review-fixture", FetchedDocument("https://fixture.invalid/review", body.encode()), ParsedDocument("Review", body))
    store.save_extraction(article["id"], 1, "fixture-model", "digest", {
        "evidence": [{"quote": body, "start": 0, "end": len(body)}],
        "cards": [{"id": "card-one", "title": "Fact", "content": "Original extracted content", "evidence_indices": [0]}],
    })
    all_ids = [row["target_type"] + ":" + row["record"]["id"] for row in store.pending_reviews(limit=1000)]
    pages = [store.pending_reviews(limit=5, offset=offset) for offset in range(0, len(all_ids), 5)]
    page_ids = [row["target_type"] + ":" + row["record"]["id"] for page in pages for row in page]
    assert page_ids == all_ids
    assert len(all_ids) == len(set(all_ids))

    card = store.get_knowledge("card-one")
    assert card["revision"] == 1
    store.update_knowledge("card-one", {"content": "First human edit"}, expected_revision=card["revision"])
    card = store.get_knowledge("card-one")
    assert card["revision"] == 2
    store.update_knowledge("card-one", {"title": "Second edit"}, expected_revision=card["revision"])
    card = store.get_knowledge("card-one")
    assert card["revision"] == 3
    assert len(card["edits"]) == 2
    assert store.list_knowledge()[0]["revision"] == 3
    challenge = store.get_entity(store.list_entities(kind="challenge")[0]["id"])
    store.review_record("entity", challenge["id"], "human_verified", note="Evidence checked", reviewer="tester", expected_revision=challenge["revision"])
    full_history = store.list_reviews(limit=1000)
    pages = [store.list_reviews(limit=1, offset=offset) for offset in range(len(full_history))]
    assert [entry["id"] for page in pages for entry in page] == [entry["id"] for entry in full_history]
    assert len({entry["id"] for entry in full_history}) == len(full_history)
