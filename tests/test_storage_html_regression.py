from pathlib import Path

from ctfka.contracts import FetchedDocument
from ctfka.ingestion import parse_document


def test_nested_navigation_removal_does_not_touch_detached_children():
    fixture = Path(__file__).parent / "fixtures" / "storage_nested_nav.html"
    parsed = parse_document(
        FetchedDocument("https://example.test", fixture.read_bytes(), "text/html")
    )
    assert parsed.title == "Parser regression"
    assert "Preserve this article content." in parsed.content
    assert "discarded nested link" not in parsed.content
