"""CTFtime source adapter tests (offline fixtures)."""

from __future__ import annotations

import asyncio

import pytest

from ctfka.contracts import DiscoveredItem, FetchedDocument
from ctfka.crawler.http import FetchError
from ctfka.crawler.registry import AdapterRegistry
from ctfka.crawler.sources.ctftime import CTFtimeAdapter
from tests.source_fixtures import FixtureClient, FixtureResponse, fixture_bytes, make_config

LIST1 = "https://ctftime.org/writeups"
LIST2 = "https://ctftime.org/writeups?page=2"


def _client() -> FixtureClient:
    return FixtureClient(
        {
            LIST1: fixture_bytes("ctftime", "list_page1.html"),
            LIST2: fixture_bytes("ctftime", "list_page2.html"),
        },
        end_status=404,
    )


def _adapter(client: FixtureClient, **kwargs) -> CTFtimeAdapter:
    return CTFtimeAdapter(make_config("ctftime", **kwargs), client)


@pytest.mark.asyncio
async def test_discovery_pages_through_listing_with_stable_identity():
    client = _client()
    adapter = _adapter(client)

    items = [item async for item in adapter.discover()]

    # Two fixture pages, then one probe page that answers 404 and ends the listing.
    assert client.calls == [LIST1, LIST2, "https://ctftime.org/writeups?page=3"]
    assert len(items) == 60
    # Identity is derived from the writeup id, never from the page it appeared on.
    assert items[0].identity == "ctftime:writeup:41140"
    assert items[0].url == "https://ctftime.org/writeup/41140"
    assert all(item.identity.startswith("ctftime:writeup:") for item in items)
    assert len({item.identity for item in items}) == len(items)
    assert adapter.discovery_complete is True


@pytest.mark.asyncio
async def test_listing_metadata_carries_event_task_topics_and_team():
    adapter = _adapter(_client())

    first = await anext(adapter.discover())

    assert first.metadata["event_name"] == "JerseyCTF VI"
    assert first.metadata["event_id"] == "3117"
    assert first.metadata["task_name"] == "Relay Artifact TX-7"
    assert first.metadata["task_id"] == "32180"
    assert first.metadata["topics"] == ["cryptography", "gcd", "rsa"]
    assert first.metadata["team"] == "NYUSEC"


@pytest.mark.asyncio
async def test_max_items_truncates_without_claiming_completeness():
    adapter = _adapter(_client(), max_items=2)

    items = [item async for item in adapter.discover()]

    assert len(items) == 2
    # A truncated scan must not advertise a complete listing.
    assert adapter.discovery_complete is False
    assert adapter.state["complete"] is False
    assert adapter.state["next_page"] == 1


@pytest.mark.asyncio
async def test_repeated_discovery_yields_identical_identities():
    first = [item async for item in _adapter(_client()).discover()]
    second = [item async for item in _adapter(_client()).discover()]

    assert [item.identity for item in first] == [item.identity for item in second]


@pytest.mark.asyncio
async def test_404_page_marks_listing_complete_instead_of_failing():
    class OnePageClient(FixtureClient):
        async def get(self, url, headers=None):
            self.calls.append(url)
            if url == LIST1:
                return FixtureResponse(fixture_bytes("ctftime", "list_page1.html"), url)
            raise FetchError("not found", category="permanent", status_code=404)

    adapter = _adapter(OnePageClient())

    items = [item async for item in adapter.discover()]

    assert len(items) == 30
    assert adapter.discovery_complete is True
@pytest.mark.asyncio
async def test_transient_listing_error_propagates_for_durable_retry():
    class BrokenClient(FixtureClient):
        async def get(self, url, headers=None):
            raise FetchError("boom", category="network")

    with pytest.raises(FetchError):
        [item async for item in _adapter(BrokenClient()).discover()]


@pytest.mark.asyncio
async def test_detail_extracts_description_and_preserves_code_fences():
    adapter = _adapter(_client())
    document = FetchedDocument(
        "https://ctftime.org/writeup/41140",
        fixture_bytes("ctftime", "writeup_41140.html"),
        "text/html",
        {"identity": "ctftime:writeup:41140", "task_name": "Relay Artifact TX-7"},
    )

    parsed = adapter.normalize(adapter.parse(document))
    adapter.validate(parsed)

    assert parsed.title == "Relay Artifact TX-7"
    # Body starts at the writeup, not at CTFtime navigation.
    assert parsed.content.startswith("# Relay Artifact TX-7")
    assert "CTFs" not in parsed.content
    assert "Upcoming" not in parsed.content
    assert parsed.content.count("```") >= 4
    assert "shared factor" in parsed.content
    assert parsed.metadata["has_body"] is True


@pytest.mark.asyncio
async def test_detail_with_only_external_link_records_the_target():
    adapter = _adapter(_client())
    document = FetchedDocument(
        "https://ctftime.org/writeup/41100",
        fixture_bytes("ctftime", "writeup_41100.html"),
        "text/html",
        {"identity": "ctftime:writeup:41100"},
    )

    parsed = adapter.normalize(adapter.parse(document))

    urls = parsed.metadata.get("original_writeup_urls") or []
    assert any("cybersecurityelite.com" in url for url in urls)


@pytest.mark.asyncio
async def test_empty_description_is_rejected_as_invalid():
    adapter = _adapter(_client())
    document = FetchedDocument(
        "https://ctftime.org/writeup/41140", b"<html><title>x</title><body></body></html>",
        "text/html", {"identity": "ctftime:writeup:41140"},
    )

    parsed = adapter.parse(document)

    with pytest.raises(ValueError, match="no body"):
        adapter.validate(parsed)


@pytest.mark.asyncio
async def test_fetch_returns_raw_bytes_and_keeps_identity_metadata():
    client = _client()
    client.add("https://ctftime.org/writeup/41140", fixture_bytes("ctftime", "writeup_41140.html"))
    adapter = _adapter(client)
    item = DiscoveredItem(
        url="https://ctftime.org/writeup/41140",
        identity="ctftime:writeup:41140",
        metadata={"task_name": "Relay Artifact TX-7"},
    )

    document = await adapter.fetch(item)

    assert document.raw == fixture_bytes("ctftime", "writeup_41140.html")
    assert document.content_type == "text/html"
    assert document.metadata["identity"] == "ctftime:writeup:41140"
    assert document.metadata["task_name"] == "Relay Artifact TX-7"


def test_adapter_is_registered_under_its_builtin_name():
    registry = AdapterRegistry()

    assert "ctftime" in registry.names()
    adapter = registry.create(
        "ctftime", make_config("ctftime"), FixtureClient()
    )
    assert isinstance(adapter, CTFtimeAdapter)


def test_invalid_listing_markup_yields_no_items():
    adapter = _adapter(FixtureClient({LIST1: b"<html><body><p>no table</p></body></html>"}))

    items = asyncio.run(_collect(adapter))

    assert items == []
    assert adapter.discovery_complete is True


async def _collect(adapter):
    return [item async for item in adapter.discover()]
