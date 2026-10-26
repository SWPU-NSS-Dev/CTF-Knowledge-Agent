"""看雪安全社区 (kanxue) source adapter tests (offline fixtures)."""

from __future__ import annotations

import pytest

from ctfka.contracts import DiscoveredItem, FetchedDocument
from ctfka.crawler.http import FetchError
from ctfka.crawler.registry import AdapterRegistry
from ctfka.crawler.sources.kanxue import KanxueAdapter
from tests.source_fixtures import FixtureClient, fixture_bytes, make_config

BOARD = "https://bbs.kanxue.com/forum-171.htm"
BOARD_P2 = "https://bbs.kanxue.com/forum-171-2.htm"
BOARD_P3 = "https://bbs.kanxue.com/forum-171-3.htm"


def _client() -> FixtureClient:
    return FixtureClient(
        {
            BOARD: fixture_bytes("kanxue", "forum_171.html"),
            BOARD_P2: fixture_bytes("kanxue", "forum_171_page2.html"),
            BOARD_P3: fixture_bytes("kanxue", "forum_171_last.html"),
        },
        end_status=404,
    )


def _adapter(client: FixtureClient, **kwargs) -> KanxueAdapter:
    options = {"forum_ids": [171]}
    options.update(kwargs.pop("options", {}))
    return KanxueAdapter(make_config("kanxue", options=options, **kwargs), client)


@pytest.mark.asyncio
async def test_board_pagination_walks_pages_and_yields_unique_threads():
    client = _client()
    adapter = _adapter(client)

    items = [item async for item in adapter.discover()]

    assert client.calls[0] == BOARD
    assert client.calls[1] == BOARD_P2
    assert len(items) == 81
    assert len({item.identity for item in items}) == 81
    assert all(item.identity.startswith("kanxue:thread:") for item in items)
    assert all(item.url.startswith("https://bbs.kanxue.com/thread-") for item in items)
    assert adapter.state["complete"] is True


@pytest.mark.asyncio
async def test_thread_identity_is_the_numeric_thread_id():
    adapter = _adapter(_client())

    first = await anext(adapter.discover())

    assert first.identity == "kanxue:thread:293322"
    assert first.url == "https://bbs.kanxue.com/thread-293322.htm"
    assert first.metadata["thread_id"] == "293322"
    assert first.metadata["forum_id"] == 171
    assert first.metadata["title"]


@pytest.mark.asyncio
async def test_repeated_pages_terminate_the_board():
    """Out-of-range pages replay the last page; that must end traversal."""
    client = FixtureClient(
        {
            BOARD: fixture_bytes("kanxue", "forum_171.html"),
            BOARD_P2: fixture_bytes("kanxue", "forum_171_page2.html"),
            BOARD_P3: fixture_bytes("kanxue", "forum_171_last.html"),
            "https://bbs.kanxue.com/forum-171-4.htm": fixture_bytes("kanxue", "forum_171_last.html"),
            "https://bbs.kanxue.com/forum-171-5.htm": fixture_bytes("kanxue", "forum_171_last.html"),
        }
    )
    adapter = _adapter(client)

    items = [item async for item in adapter.discover()]

    # 32 + 27 + 22 unique threads; the repeated last page adds nothing.
    assert len(items) == 81
    assert len({item.identity for item in items}) == 81
    assert len(client.calls) == 4


@pytest.mark.asyncio
async def test_max_items_truncates_and_records_resume_point():
    adapter = _adapter(_client(), max_items=5)

    items = [item async for item in adapter.discover()]

    assert len(items) == 5
    assert adapter.state["complete"] is False
    assert adapter.state["next_board"] == 0
    # Resume inside the truncated page so already-queued threads are not repeated.
    assert adapter.state["next_page"] == 1
    assert adapter.state["next_index"] == 5


@pytest.mark.asyncio
async def test_resume_continues_from_recorded_position():
    adapter = _adapter(_client(), max_items=5)
    first = [item async for item in adapter.discover()]
    state = adapter.state

    client2 = _client()
    resumed = _adapter(client2)
    resumed.set_state(state)
    second = [item async for item in resumed.discover()]

    assert not {item.identity for item in first} & {item.identity for item in second}


@pytest.mark.asyncio
async def test_empty_board_ends_cleanly():
    adapter = _adapter(
        FixtureClient({BOARD: b"<html><body><p>no threads</p></body></html>"}), max_items=None
    )

    items = [item async for item in adapter.discover()]

    assert items == []


@pytest.mark.asyncio
async def test_missing_board_page_ends_that_board():
    class Missing(FixtureClient):
        async def get(self, url, headers=None):
            self.calls.append(url)
            if url == BOARD:
                return await super().get(url, headers)
            raise FetchError("gone", category="permanent", status_code=404)

    adapter = _adapter(Missing({BOARD: fixture_bytes("kanxue", "forum_171.html")}))

    items = [item async for item in adapter.discover()]

    assert len(items) == 32


@pytest.mark.asyncio
async def test_transient_board_error_propagates():
    class Broken(FixtureClient):
        async def get(self, url, headers=None):
            raise FetchError("boom", category="network")

    with pytest.raises(FetchError):
        [item async for item in _adapter(Broken()).discover()]


@pytest.mark.asyncio
async def test_multiple_boards_are_all_traversed():
    client = FixtureClient(
        {
            "https://bbs.kanxue.com/forum-4.htm": fixture_bytes("kanxue", "forum_171.html"),
            "https://bbs.kanxue.com/forum-4-2.htm": fixture_bytes("kanxue", "forum_171_page2.html"),
            "https://bbs.kanxue.com/forum-150.htm": fixture_bytes("kanxue", "forum_171_last.html"),
        },
        end_status=404,
    )
    adapter = KanxueAdapter(
        make_config("kanxue", options={"forum_ids": [4, 150]}), client
    )

    items = [item async for item in adapter.discover()]

    forums = {item.metadata["forum_id"] for item in items}
    assert forums == {4, 150}
    assert len(items) == 32 + 27 + 22


@pytest.mark.asyncio
async def test_first_post_body_is_extracted_with_code_fences():
    adapter = _adapter(_client())
    document = FetchedDocument(
        "https://bbs.kanxue.com/thread-293255.htm",
        fixture_bytes("kanxue", "thread_293255.html"),
        "text/html",
        {"identity": "kanxue:thread:293255"},
    )

    parsed = adapter.normalize(adapter.parse(document))
    adapter.validate(parsed)

    assert "IDA Pro 9.4" in parsed.title
    assert parsed.content.startswith("![](")
    assert "Python 3.11.9" in parsed.content
    # Code/verbatim blocks and encrypted link payloads are kept as inert text.
    assert parsed.content.count("```") >= 4
    assert parsed.metadata["encrypted_link_blocks"] >= 1


@pytest.mark.asyncio
async def test_encrypted_links_are_preserved_verbatim_not_executed():
    adapter = _adapter(_client())
    document = FetchedDocument(
        "https://bbs.kanxue.com/thread-291572.htm",
        fixture_bytes("kanxue", "thread_293255.html"),
        "text/html",
        {"identity": "kanxue:thread:293255"},
    )

    parsed = adapter.normalize(adapter.parse(document))

    # The payload appears inside a fenced text block, so nothing can execute it.
    assert "```text" in parsed.content
    for block in parsed.content.split("```text")[1:]:
        assert block.split("```", 1)[0].strip()


@pytest.mark.asyncio
async def test_thread_without_first_post_is_rejected():
    adapter = _adapter(_client())
    document = FetchedDocument(
        "https://bbs.kanxue.com/thread-1.htm",
        "<html><head><title>标题-板块-看雪安全社区</title></head><body></body></html>".encode(),
        "text/html",
        {"identity": "kanxue:thread:1"},
    )

    with pytest.raises(ValueError, match="no first-post body"):
        adapter.validate(adapter.parse(document))


@pytest.mark.asyncio
async def test_fetch_preserves_raw_bytes_and_identity():
    client = _client()
    client.add("https://bbs.kanxue.com/thread-293255.htm", fixture_bytes("kanxue", "thread_293255.html"))
    adapter = _adapter(client)
    item = DiscoveredItem(
        url="https://bbs.kanxue.com/thread-293255.htm",
        identity="kanxue:thread:293255",
        metadata={"thread_id": "293255", "forum_id": 171},
    )

    document = await adapter.fetch(item)

    assert document.raw == fixture_bytes("kanxue", "thread_293255.html")
    assert document.metadata["identity"] == "kanxue:thread:293255"
    assert document.metadata["forum_id"] == 171


def test_adapter_is_registered_under_its_builtin_name():
    registry = AdapterRegistry()

    assert "kanxue" in registry.names()
    assert isinstance(
        registry.create("kanxue", make_config("kanxue"), FixtureClient()), KanxueAdapter
    )


def test_non_numeric_forum_id_is_rejected():
    adapter = KanxueAdapter(
        make_config("kanxue", options={"forum_ids": ["abc"]}), FixtureClient()
    )
    with pytest.raises(ValueError, match="forum id must be numeric"):
        adapter._boards()
