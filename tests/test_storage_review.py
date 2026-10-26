from ctfka.contracts import FetchedDocument
from ctfka.models import Store
from ctfka.models.store import _simhash


def test_negative_signed_simhash_same_content_has_zero_distance(tmp_path):
    content = "negative simhash regression"
    assert _simhash(content) < 0
    store = Store(tmp_path)
    store.ingest(
        "s",
        FetchedDocument("https://example.test/one", content.encode(), metadata={"identity": "one"}),
    )
    second = store.ingest(
        "s",
        FetchedDocument("https://example.test/two", content.encode(), metadata={"identity": "two"}),
    )
    candidate = next(item for item in second["near_duplicates"] if item["title"] == content)
    assert candidate["simhash_distance"] == 0


def test_article_search_metadata_never_implies_human_verification(tmp_path):
    store = Store(tmp_path)
    article = store.ingest("s", FetchedDocument("https://example.test", b"evidence"))
    result = store.get_article(article["id"])
    assert result["quality_status"] == "source_claimed"
    assert result["verification_status"] == "unverified"
