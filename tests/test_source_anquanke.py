"""安全客 (anquanke) source adapter tests (offline fixtures)."""

from __future__ import annotations

import pytest

from ctfka.contracts import DiscoveredItem, FetchedDocument
from ctfka.crawler.http import FetchError
from ctfka.crawler.registry import AdapterRegistry
from ctfka.crawler.sources.anquanke import AnquankeAdapter
from tests.source_fixtures import FixtureClient, fixture_bytes, make_config

INDEX = "https://www.anquanke.com/sitemap.xml"
CHILD1 = "https://www.anquanke.com/sitemap1.xml"
CHILD2 = "https://www.anquanke.com/sitemap2.xml"
CHILD3 = "https://www.anquanke.com/sitemap3.xml"
# Real post ids, three per child sitemap.
POSTS = {
    CHILD1: ("316355", "316350", "316346"),
    CHILD2: ("316272", "316257", "316251"),
    CHILD3: ("316248", "316245", "316231"),
}
ALL_POSTS = tuple(pid for ids in POSTS.values() for pid in ids)


def _client() -> FixtureClient:
    return FixtureClient(
        {
            INDEX: fixture_bytes("anquanke", "sitemap_index.xml"),
            CHILD1: fixture_bytes("anquanke", "sitemap1.xml"),
            CHILD2: fixture_bytes("anquanke", "sitemap2.xml"),
            CHILD3: fixture_bytes("anquanke", "sitemap3.xml"),
        }
    )


def _adapter(client: FixtureClient, **kwargs) -> AnquankeAdapter:
    return AnquankeAdapter(make_config("anquanke", **kwargs), client)


@pytest.mark.asyncio
async def test_sitemap_index_is_traversed_and_posts_are_collected():
    client = _client()
    adapter = _adapter(client)

    items = [item async for item in adapter.discover()]

    assert client.calls[0] == INDEX
    assert len(items) == 9
    assert [item.identity for item in items] == [
        f"anquanke:post:{pid}" for pid in ALL_POSTS
    ]
    assert all(item.url.startswith("https://www.anquanke.com/post/id/") for item in items)
    assert adapter.state["complete"] is True


@pytest.mark.asyncio
async def test_each_child_sitemap_records_its_own_lastmod_watermark():
    adapter = _adapter(_client())

    [item async for item in adapter.discover()]

    watermarks = adapter.state["lastmod"]
    # Only urlset children carry entries and therefore watermarks.
    assert set(watermarks) == {CHILD1, CHILD2, CHILD3}
    # Watermarks follow each child's newest entry, not a global maximum.
    assert watermarks[CHILD1] == "2026-10-26T14:54:01+08:00"
    assert watermarks[CHILD2] == "2026-10-22T15:57:59+08:00"
    assert watermarks[CHILD3] == "2026-10-19T12:00:22+08:00"


@pytest.mark.asyncio
async def test_second_pass_reports_no_new_posts_once_watermarks_are_recorded():
    adapter = _adapter(_client())
    first = [item async for item in adapter.discover()]
    state = adapter.state

    second_adapter = _adapter(_client())
    second_adapter.set_state(state)
    second = [item async for item in second_adapter.discover()]

    assert len(first) == 9
    assert second == []
    assert second_adapter.state["complete"] is True


@pytest.mark.asyncio
async def test_post_with_newer_lastmod_is_yielded_again():
    adapter = _adapter(_client())
    [item async for item in adapter.discover()]
    state = adapter.state

    newer = (
        b'<?xml version="1.0" encoding="UTF-8"?>'
        b'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        b"<url><loc>https://www.anquanke.com/post/id/316355</loc>"
        b"<lastmod>2099-01-01T00:00:00+08:00</lastmod></url>"
        b"</urlset>"
    )
    updated = FixtureClient(
        {
            INDEX: fixture_bytes("anquanke", "sitemap_index.xml"),
            CHILD1: newer,
            CHILD2: fixture_bytes("anquanke", "sitemap2.xml"),
            CHILD3: fixture_bytes("anquanke", "sitemap3.xml"),
        }
    )
    resumed = _adapter(updated)
    resumed.set_state(state)

    items = [item async for item in resumed.discover()]

    assert [item.identity for item in items] == ["anquanke:post:316355"]
    assert items[0].metadata["lastmod"] == "2099-01-01T00:00:00+08:00"


@pytest.mark.asyncio
async def test_max_items_truncates_without_marking_complete_and_resumes():
    adapter = _adapter(_client(), max_items=4)

    items = [item async for item in adapter.discover()]

    assert len(items) == 4
    assert adapter.state["complete"] is False
    # A truncated child must not record a watermark that would skip its remainder.
    assert CHILD2 not in adapter.state["lastmod"]
    assert adapter.state["resume_url"] == CHILD2
    assert adapter.state["resume_index"] == 1

    resumed = _adapter(_client(), max_items=5)
    resumed.set_state(adapter.state)
    rest = [item async for item in resumed.discover()]

    assert not {item.identity for item in items} & {item.identity for item in rest}
    assert {item.identity for item in items} | {item.identity for item in rest} == {
        f"anquanke:post:{pid}" for pid in ALL_POSTS
    }


@pytest.mark.asyncio
async def test_retired_sitemap_child_does_not_abort_the_traversal():
    class RetiredChild(FixtureClient):
        async def get(self, url, headers=None):
            self.calls.append(url)
            if url == CHILD2:
                raise FetchError("gone", category="permanent", status_code=404)
            return await super().get(url, headers)

    adapter = _adapter(RetiredChild(_client().mapping))

    items = [item async for item in adapter.discover()]

    assert len(items) == 6
    assert {item.identity for item in items} == {
        f"anquanke:post:{pid}" for pid in POSTS[CHILD1] + POSTS[CHILD3]
    }


@pytest.mark.asyncio
async def test_transient_sitemap_error_is_not_swallowed():
    class BrokenChild(FixtureClient):
        async def get(self, url, headers=None):
            self.calls.append(url)
            if url == CHILD2:
                raise FetchError("boom", category="network")
            return await super().get(url, headers)

    with pytest.raises(FetchError):
        [item async for item in _adapter(BrokenChild(_client().mapping)).discover()]


@pytest.mark.asyncio
async def test_malformed_sitemap_raises_a_source_defect():
    adapter = _adapter(FixtureClient({INDEX: b"<not-xml"}))
    with pytest.raises(ValueError, match="invalid anquanke sitemap"):
        [item async for item in adapter.discover()]


@pytest.mark.asyncio
async def test_untrusted_cross_origin_sitemap_child_is_ignored():
    cross = (
        b'<?xml version="1.0" encoding="UTF-8"?>'
        b'<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        b"<sitemap><loc>https://evil.example/steal.xml</loc></sitemap>"
        b"</sitemapindex>"
    )
    client = FixtureClient({INDEX: cross})

    items = [item async for item in _adapter(client).discover()]

    assert items == []
    assert not any("evil.example" in url for url in client.calls)


def _detail(post_id: str) -> FetchedDocument:
    return FetchedDocument(
        f"https://www.anquanke.com/post/id/{post_id}",
        fixture_bytes("anquanke", f"post_{post_id}.html"),
        "text/html",
        {"identity": f"anquanke:post:{post_id}"},
    )


@pytest.mark.asyncio
async def test_article_body_is_extracted_without_site_chrome():
    adapter = _adapter(_client(), max_items=1)

    parsed = adapter.normalize(adapter.parse(_detail("316355")))
    adapter.validate(parsed)

    assert "Node.js" in parsed.title
    assert "安全资讯平台" not in parsed.title
    # Navigation labels must not appear inside the stored body.
    assert "安全资讯" not in parsed.content
    assert "专栏" not in parsed.content
    assert parsed.content.startswith("Google Brotli解码器")
    assert "CVE-2026-111497" in parsed.content
    # Code blocks survive extraction.
    assert parsed.content.count("```") >= 2


@pytest.mark.asyncio
async def test_article_tags_and_headings_are_preserved():
    adapter = _adapter(_client(), max_items=1)

    parsed = adapter.normalize(adapter.parse(_detail("315457")))

    assert "Hypervisor" in parsed.title
    assert "Denuvo" in parsed.content
    assert parsed.metadata["tags"]
    assert any("hypervisor" in tag.lower() for tag in parsed.metadata["tags"])
    # Headings inside the article body are retained.
    assert "### **摘要**" in parsed.content


@pytest.mark.asyncio
async def test_login_gated_stub_is_rejected():
    adapter = _adapter(_client(), max_items=1)
    stub = (
        "<html><head><title>t-安全KER - 安全资讯平台</title></head><body>"
        "<h1>t</h1><div class='content' id='js-article'>"
        "<p>登录吧！</p></div></body></html>"
    ).encode()
    document = FetchedDocument("https://www.anquanke.com/post/id/1", stub, "text/html", {})

    parsed = adapter.parse(document)

    assert parsed.metadata.get("login_gated") is True
    with pytest.raises(ValueError, match="requires login"):
        adapter.validate(parsed)


@pytest.mark.asyncio
async def test_missing_body_container_is_rejected():
    adapter = _adapter(_client(), max_items=1)
    document = FetchedDocument(
        "https://www.anquanke.com/post/id/1",
        b"<html><body><h1>T</h1></body></html>",
        "text/html",
        {},
    )

    with pytest.raises(ValueError, match="#js-article is empty"):
        adapter.validate(adapter.parse(document))


@pytest.mark.asyncio
async def test_fetch_preserves_raw_bytes_and_identity():
    client = _client()
    client.add(
        "https://www.anquanke.com/post/id/316355", fixture_bytes("anquanke", "post_316355.html")
    )
    adapter = _adapter(client)
    item = DiscoveredItem(
        url="https://www.anquanke.com/post/id/316355",
        identity="anquanke:post:316355",
        metadata={"post_id": "316355"},
    )

    document = await adapter.fetch(item)

    assert document.raw == fixture_bytes("anquanke", "post_316355.html")
    assert document.metadata["identity"] == "anquanke:post:316355"
    assert document.metadata["post_id"] == "316355"


def test_adapter_is_registered_under_its_builtin_name():
    registry = AdapterRegistry()

    assert "anquanke" in registry.names()
    assert isinstance(
        registry.create("anquanke", make_config("anquanke"), FixtureClient()),
        AnquankeAdapter,
    )
