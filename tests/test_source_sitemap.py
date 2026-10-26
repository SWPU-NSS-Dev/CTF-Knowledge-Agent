"""Generic sitemap adapter tests (offline fixtures).

The fixtures use the namespaced shapes real sitemaps publish: a ``sitemapindex``
whose children are ``urlset`` files, entries with ``lastmod`` in several encodings
(offset, ``Z``, date-only, space-separated, missing, unusable), ``xhtml:link``
alternates, and a ``#fragment`` that must be defragmented before fetching.
"""

from __future__ import annotations

import pytest

from ctfka.contracts import DiscoveredItem, FetchedDocument
from ctfka.crawler.http import FetchError
from ctfka.crawler.registry import AdapterRegistry
from ctfka.crawler.sources.sitemap import SitemapAdapter
from tests.source_fixtures import FixtureClient, fixture_bytes, make_config

INDEX = "https://docs.example.org/sitemap.xml"
POSTS1 = "https://docs.example.org/sitemap-posts-1.xml"
POSTS2 = "https://docs.example.org/sitemap-posts-2.xml"
PAGES = "https://docs.example.org/sitemap-pages.xml"
RETIRED = "https://docs.example.org/sitemap-retired.xml"

# Entries carried by sitemap-posts-2.xml, in document order.
POSTS2_URLS = [
    "https://docs.example.org/posts/2026/pwn-toolchain-setup",
    "https://docs.example.org/posts/2026/awdp-hardening-checklist",
    "https://docs.example.org/posts/2026/kernel-modprobe-path",
]

# Entries carried by sitemap-posts-1.xml, in document order.
POSTS1_URLS = [
    "https://docs.example.org/posts/2026/stack-overflow-primer",
    "https://docs.example.org/posts/2026/format-string-basics",
    "https://docs.example.org/posts/2026/heap-notes",  # #allocator removed
    "https://docs.example.org/posts/2025/older-but-valid",
    "https://docs.example.org/posts/2026/no-lastmod-entry",
]


def _client(**overrides: bytes) -> FixtureClient:
    mapping = {
        INDEX: fixture_bytes("sitemap", "sitemap_index.xml"),
        POSTS1: fixture_bytes("sitemap", "sitemap_posts_1.xml"),
        POSTS2: fixture_bytes("sitemap", "sitemap_posts_2.xml"),
        PAGES: fixture_bytes("sitemap", "sitemap_pages.xml"),
    }
    mapping.update(overrides)
    return FixtureClient(mapping)


def _adapter(client: FixtureClient, **kwargs) -> SitemapAdapter:
    options = kwargs.pop("options", {}) or {}
    options.setdefault("sitemap_urls", [INDEX])
    return SitemapAdapter(make_config("sitemap", options=options, **kwargs), client)


@pytest.mark.asyncio
async def test_sitemap_index_children_are_traversed_in_order():
    client = _client()

    items = [item async for item in _adapter(client).discover()]

    assert client.calls == [INDEX, POSTS1, PAGES, POSTS2]
    assert [item.url for item in items[:5]] == POSTS1_URLS
    # The same page listed in two children is yielded once.
    urls = [item.url for item in items]
    assert len(urls) == len(set(urls))
    assert "https://docs.example.org/about" in urls


@pytest.mark.asyncio
async def test_identity_is_the_defragmented_url():
    items = [item async for item in _adapter(_client()).discover()]

    heap = next(item for item in items if "heap-notes" in item.url)
    assert heap.url == "https://docs.example.org/posts/2026/heap-notes"
    assert heap.identity == heap.url
    # Every identity is stable and unique across the pass.
    assert len({item.identity for item in items}) == len(items)


@pytest.mark.asyncio
async def test_lastmod_is_carried_as_metadata_and_cross_origin_child_skipped():
    items = [item async for item in _adapter(_client()).discover()]

    stack = next(item for item in items if item.url.endswith("stack-overflow-primer"))
    assert stack.metadata["lastmod"] == "2026-10-26T14:54:01+08:00"
    assert stack.metadata["sitemap"] == POSTS1
    assert stack.metadata["source"] == "sitemap"
    # Entries without a usable lastmod are still yielded, minus the bogus key.
    bare = next(item for item in items if item.url.endswith("no-lastmod-entry"))
    assert "lastmod" not in bare.metadata


@pytest.mark.asyncio
async def test_include_and_exclude_filters_apply():
    items = [
        item
        async for item in _adapter(
            _client(), options={"include_regex": ["/posts/"], "exclude_regex": ["/2025/"]}
        ).discover()
    ]

    assert items
    assert all("/posts/" in item.url for item in items)
    assert not any("/2025/" in item.url for item in items)


@pytest.mark.asyncio
async def test_invalid_filter_regex_is_rejected():
    adapter = _adapter(_client(), options={"include_regex": ["("]})

    with pytest.raises(ValueError, match="invalid sitemap include URL filter regex"):
        [item async for item in adapter.discover()]


@pytest.mark.asyncio
async def test_missing_sitemap_urls_is_a_configuration_error():
    adapter = SitemapAdapter(make_config("sitemap", options={}), FixtureClient({}))

    with pytest.raises(ValueError, match="requires options.sitemap_urls"):
        [item async for item in adapter.discover()]


@pytest.mark.asyncio
async def test_max_items_truncates_without_claiming_complete():
    adapter = _adapter(_client(), max_items=2)

    items = [item async for item in adapter.discover()]

    assert [item.url for item in items] == POSTS1_URLS[:2]
    assert adapter.state["complete"] is False
    # A truncated child must not be recorded as finished, and no watermark may be
    # committed for it, otherwise the tail of that child would be lost forever.
    assert POSTS1 not in adapter.state.get("done_sitemaps", [])
    assert POSTS1 not in adapter.state["lastmod"]


@pytest.mark.asyncio
async def test_truncated_pass_resumes_inside_the_same_child():
    adapter = _adapter(_client(), max_items=2)
    first = [item async for item in adapter.discover()]
    assert len(first) == 2
    assert adapter.state["resume_url"] == POSTS1
    assert adapter.state["resume_index"] == 2

    resumed = _adapter(_client())
    resumed.set_state(adapter.state)
    second = [item async for item in resumed.discover()]

    # The tail of the truncated child is continued from the saved index.
    assert [item.url for item in second[:3]] == POSTS1_URLS[2:]
    assert "https://docs.example.org/about" in [item.url for item in second]
    assert resumed.state["complete"] is True
    # Nothing is lost: every entry of the source is covered by the two passes.
    # ``stack-overflow-primer`` is re-offered because the truncated pass never
    # reached the child that lists it too, and cross-child deduplication is
    # per-pass; ``Store`` keys articles by identity, so this is idempotent.
    covered = {item.url for item in first} | {item.url for item in second}
    assert covered == set(POSTS1_URLS + ["https://docs.example.org/about"] + POSTS2_URLS)
    # Every entry the truncated pass yielded came from the child it was reading.
    assert {item.url for item in first} <= set(POSTS1_URLS)


@pytest.mark.asyncio
async def test_second_pass_only_yields_entries_at_or_above_the_watermark():
    adapter = _adapter(_client())
    first = [item async for item in adapter.discover()]
    state = adapter.state
    assert state["complete"] is True
    assert state["lastmod"][POSTS1] == "2026-10-26T14:54:01+08:00"

    second_adapter = _adapter(_client())
    second_adapter.set_state(state)
    second = [item async for item in second_adapter.discover()]

    # Entries older than the watermark are not offered again.
    assert first
    reoffered = {item.url for item in second}
    assert "https://docs.example.org/posts/2025/older-but-valid" not in reoffered
    assert "https://docs.example.org/posts/2026/format-string-basics" not in reoffered
    # Entries with no usable lastmod cannot be proven unchanged, so they are always
    # re-offered and left to Store idempotence. Same for the unparseable one.
    assert "https://docs.example.org/posts/2026/no-lastmod-entry" in reoffered
    assert "https://docs.example.org/posts/2026/kernel-modprobe-path" in reoffered
    assert second_adapter.state["complete"] is True


@pytest.mark.asyncio
async def test_edited_entry_is_republished_on_the_next_pass():
    bumped = fixture_bytes("sitemap", "sitemap_posts_1.xml").replace(
        b"<lastmod>2026-10-25T09:00:00Z</lastmod>",
        b"<lastmod>2026-10-27T10:00:00Z</lastmod>",
    )
    adapter = _adapter(_client())
    [item async for item in adapter.discover()]
    state = adapter.state

    edited = _adapter(_client(**{POSTS1: bumped}))
    edited.set_state(state)
    second = [item async for item in edited.discover()]

    # The entry whose lastmod advanced is handed over again…
    assert "https://docs.example.org/posts/2026/heap-notes" in {item.url for item in second}
    # …while unchanged entries with a usable lastmod stay behind the watermark.
    assert "https://docs.example.org/posts/2025/older-but-valid" not in {
        item.url for item in second
    }


@pytest.mark.asyncio
async def test_retired_child_is_skipped_instead_of_stalling_the_pass():
    index = (
        b'<?xml version="1.0" encoding="UTF-8"?>'
        b'<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        b"<sitemap><loc>" + RETIRED.encode() + b"</loc></sitemap>"
        b"<sitemap><loc>" + PAGES.encode() + b"</loc></sitemap>"
        b"</sitemapindex>"
    )

    class Retired(FixtureClient):
        def __init__(self):
            super().__init__(
                {INDEX: index, PAGES: fixture_bytes("sitemap", "sitemap_pages.xml")}
            )

        async def get(self, url, headers=None):
            if url == RETIRED:
                self.calls.append(url)
                raise FetchError("gone", category="permanent", status_code=404)
            return await super().get(url, headers)

    client = Retired()
    items = [item async for item in _adapter(client).discover()]

    assert RETIRED in client.calls
    assert "https://docs.example.org/about" in [item.url for item in items]


@pytest.mark.asyncio
async def test_transient_fetch_error_propagates():
    class Broken(FixtureClient):
        async def get(self, url, headers=None):
            raise FetchError("boom", category="network")

    with pytest.raises(FetchError):
        [item async for item in _adapter(Broken()).discover()]


@pytest.mark.asyncio
async def test_malformed_sitemap_raises_a_source_defect():
    adapter = _adapter(_client(**{POSTS1: b"<not-xml"}), options={"sitemap_urls": [POSTS1]})

    with pytest.raises(ValueError, match="invalid sitemap at"):
        [item async for item in adapter.discover()]


@pytest.mark.asyncio
async def test_unexpected_root_element_is_rejected():
    adapter = _adapter(
        _client(**{POSTS1: b"<rss><channel/></rss>"}), options={"sitemap_urls": [POSTS1]}
    )

    with pytest.raises(ValueError, match="must be urlset or sitemapindex"):
        [item async for item in adapter.discover()]


@pytest.mark.asyncio
async def test_cross_origin_child_is_not_fetched_by_default():
    index = (
        b'<?xml version="1.0" encoding="UTF-8"?>'
        b'<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        b"<sitemap><loc>https://evil.example/sitemap.xml</loc></sitemap>"
        b"</sitemapindex>"
    )
    client = FixtureClient({INDEX: index})

    items = [item async for item in _adapter(client).discover()]

    assert items == []
    assert not any("evil.example" in url for url in client.calls)


@pytest.mark.asyncio
async def test_fetch_preserves_raw_bytes_and_identity():
    page = b"<html><body><h1>Stack overflow primer</h1><p>body</p></body></html>"
    client = _client(**{"https://docs.example.org/posts/2026/stack-overflow-primer": page})
    adapter = _adapter(client)
    item = DiscoveredItem(
        url=POSTS1_URLS[0], identity=POSTS1_URLS[0], metadata={"lastmod": "2026-10-26"}
    )

    document = await adapter.fetch(item)

    assert document.raw == page
    assert document.content_type == "text/html"
    assert document.metadata["identity"] == POSTS1_URLS[0]


def test_parse_and_validate_use_the_shared_parser():
    adapter = _adapter(_client())
    document = FetchedDocument(
        POSTS1_URLS[0],
        b"<html><body><h1>Stack overflow primer</h1><p>Real body.</p></body></html>",
        "text/html",
        {},
    )

    parsed = adapter.normalize(adapter.parse(document))
    adapter.validate(parsed)

    assert parsed.title == "Stack overflow primer"
    assert "Real body." in parsed.content


def test_adapter_is_registered_under_its_builtin_name():
    registry = AdapterRegistry()

    assert "sitemap" in registry.names()
    assert isinstance(
        registry.create("sitemap", make_config("sitemap", options={"sitemap_urls": [INDEX]}), _client()),
        SitemapAdapter,
    )
