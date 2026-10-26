"""HackTricks source adapter tests (offline fixtures)."""

from __future__ import annotations

import pytest

from ctfka.contracts import DiscoveredItem, FetchedDocument
from ctfka.crawler.http import FetchError
from ctfka.crawler.registry import AdapterRegistry
from ctfka.crawler.sources.hacktricks import HackTricksAdapter
from tests.source_fixtures import FixtureClient, fixture_bytes, make_config

SITEMAP = "https://hacktricks.wiki/en/sitemap.xml"
PAGE = (
    "https://hacktricks.wiki/en/binary-exploitation/ios-exploiting/"
    "CVE-2021-30807-IOMobileFrameBuffer.html"
)


def _client() -> FixtureClient:
    return FixtureClient({SITEMAP: fixture_bytes("hacktricks", "sitemap_en.xml")})


def _adapter(client: FixtureClient, **kwargs) -> HackTricksAdapter:
    return HackTricksAdapter(make_config("hacktricks", **kwargs), client)


@pytest.mark.asyncio
async def test_sitemap_pages_are_discovered_with_stable_slugs():
    client = _client()
    adapter = _adapter(client)

    items = [item async for item in adapter.discover()]

    assert client.calls == [SITEMAP]
    assert len(items) == 6
    assert all(item.identity.startswith("hacktricks:en/") for item in items)
    assert len({item.identity for item in items}) == 6
    assert adapter.state["complete"] is True


@pytest.mark.asyncio
async def test_include_and_exclude_filters_are_applied():
    client = _client()
    adapter = _adapter(
        client, options={"include_regex": ["/AI/"], "exclude_regex": ["A2A"]}
    )

    items = [item async for item in adapter.discover()]

    assert items
    assert all("/AI/" in item.url for item in items)
    assert not any("A2A" in item.url for item in items)


@pytest.mark.asyncio
async def test_invalid_filter_regex_is_rejected():
    adapter = _adapter(_client(), options={"include_regex": ["("]})

    with pytest.raises(ValueError, match="invalid HackTricks URL filter regex"):
        [item async for item in adapter.discover()]


@pytest.mark.asyncio
async def test_max_items_truncates_without_claiming_complete():
    adapter = _adapter(_client(), max_items=2)

    items = [item async for item in adapter.discover()]

    assert len(items) == 2
    assert adapter.state["complete"] is False
    # The interrupted sitemap must not be recorded as finished.
    assert SITEMAP not in adapter.state["done_sitemaps"]


@pytest.mark.asyncio
async def test_second_pass_reports_already_known_pages_as_done():
    adapter = _adapter(_client())
    first = [item async for item in adapter.discover()]
    state = adapter.state

    second_adapter = _adapter(_client())
    second_adapter.set_state(state)
    second = [item async for item in second_adapter.discover()]

    assert len(first) == 6
    # Every page is already recorded, so a second pass yields nothing new.
    assert second == []
    assert second_adapter.state["complete"] is True
    assert len(second_adapter.state["pages"]) == 6


@pytest.mark.asyncio
async def test_sitemap_index_children_are_followed():
    index = (
        b'<?xml version="1.0" encoding="UTF-8"?>'
        b'<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        b"<sitemap><loc>https://hacktricks.wiki/en/sitemap-a.xml</loc></sitemap>"
        b"</sitemapindex>"
    )
    child = (
        b'<?xml version="1.0" encoding="UTF-8"?>'
        b'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        b"<url><loc>https://hacktricks.wiki/en/AI/One.html</loc></url>"
        b"</urlset>"
    )
    client = FixtureClient(
        {SITEMAP: index, "https://hacktricks.wiki/en/sitemap-a.xml": child}
    )

    items = [item async for item in _adapter(client).discover()]

    assert [item.url for item in items] == ["https://hacktricks.wiki/en/AI/One.html"]
    assert items[0].identity == "hacktricks:en/AI/One.html"


@pytest.mark.asyncio
async def test_cross_origin_sitemap_child_is_ignored():
    index = (
        b'<?xml version="1.0" encoding="UTF-8"?>'
        b'<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        b"<sitemap><loc>https://evil.example/sitemap.xml</loc></sitemap>"
        b"</sitemapindex>"
    )
    client = FixtureClient({SITEMAP: index})

    items = [item async for item in _adapter(client).discover()]

    assert items == []
    assert not any("evil.example" in url for url in client.calls)


@pytest.mark.asyncio
async def test_malformed_sitemap_raises_a_source_defect():
    adapter = _adapter(FixtureClient({SITEMAP: b"<not-xml"}))

    with pytest.raises(ValueError, match="invalid HackTricks sitemap"):
        [item async for item in adapter.discover()]


@pytest.mark.asyncio
async def test_transient_sitemap_error_propagates():
    class Broken(FixtureClient):
        async def get(self, url, headers=None):
            raise FetchError("boom", category="network")

    with pytest.raises(FetchError):
        [item async for item in _adapter(Broken()).discover()]


def _detail(raw: bytes, url: str = PAGE) -> FetchedDocument:
    return FetchedDocument(url, raw, "text/html", {"identity": "hacktricks:en/page.html"})


@pytest.mark.asyncio
async def test_body_is_extracted_from_content_main_without_mdbook_chrome():
    adapter = _adapter(_client())

    parsed = adapter.normalize(adapter.parse(_detail(fixture_bytes("hacktricks", "page_ios_cve.html"))))
    adapter.validate(parsed)

    assert parsed.title == "CVE-2021-30807: IOMobileFrameBuffer OOB"
    assert parsed.content.startswith("# CVE-2021-30807: IOMobileFrameBuffer OOB")
    # The generic body heuristic returns almost nothing for mdBook; this must be full.
    assert len(parsed.content) > 10000
    # Book chrome must not leak into the article body.
    assert "Sponsored" not in parsed.content
    assert "Translations" not in parsed.content
    assert "Learn & practice AWS Hacking" not in parsed.content
    # Heading anchors are unwrapped rather than rendered as links.
    assert "(#cve-2021-30807-iomobileframebuffer-oob)" not in parsed.content


@pytest.mark.asyncio
async def test_technical_callouts_and_code_blocks_are_kept():
    adapter = _adapter(_client())

    parsed = adapter.normalize(adapter.parse(_detail(fixture_bytes("hacktricks", "page_ios_cve.html"))))

    # note/warning callouts carry fixed versions and mitigations: keep them.
    assert "Note" in parsed.content or "Warning" in parsed.content
    assert "14.7.1" in parsed.content
    assert parsed.content.count("```") >= 4
    # Exploit code must survive extraction.
    assert "IOSurfaceRoot" in parsed.content


@pytest.mark.asyncio
async def test_links_and_reference_targets_are_preserved():
    adapter = _adapter(_client())

    parsed = adapter.normalize(adapter.parse(_detail(fixture_bytes("hacktricks", "page_ios_cve.html"))))

    # External PoC references must survive, including their targets.
    assert "github.com/saaramar/IOMobileFrameBuffer_LPE_POC" in parsed.content
    assert "[1]" in parsed.content or "](#references)" in parsed.content
    # Code fences carrying the analysis are kept.
    assert "```" in parsed.content


@pytest.mark.asyncio
async def test_redirect_stub_page_is_rejected():
    adapter = _adapter(_client())

    parsed = adapter.parse(_detail(fixture_bytes("hacktricks", "page_empty.html")))

    with pytest.raises(ValueError, match="no #content main body"):
        adapter.validate(parsed)


@pytest.mark.asyncio
async def test_fetch_preserves_raw_bytes_and_identity():
    client = _client()
    client.add(PAGE, fixture_bytes("hacktricks", "page_ios_cve.html"))
    adapter = _adapter(client)
    item = DiscoveredItem(url=PAGE, identity="hacktricks:en/page.html", metadata={})

    document = await adapter.fetch(item)

    assert document.raw == fixture_bytes("hacktricks", "page_ios_cve.html")
    assert document.content_type == "text/html"
    assert document.metadata["identity"] == "hacktricks:en/page.html"


def test_adapter_is_registered_under_its_builtin_name():
    registry = AdapterRegistry()

    assert "hacktricks" in registry.names()
    assert isinstance(
        registry.create("hacktricks", make_config("hacktricks"), FixtureClient()),
        HackTricksAdapter,
    )
