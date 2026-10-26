"""Shared helpers for the per-site source adapter tests.

All tests are offline: they replay real response bodies captured from the live
sites through a mock client, so no test touches the network.
"""

from __future__ import annotations

from pathlib import Path

from ctfka.config import SourceConfig
from ctfka.crawler.http import FetchError

FIXTURES = Path(__file__).parent / "fixtures"


class FixtureResponse:
    """Minimal stand-in for ``httpx.Response`` used by the adapters."""

    def __init__(self, content: bytes, url: str, headers: dict | None = None, status_code: int = 200):
        self.content = content
        self.url = url
        self.status_code = status_code
        self.headers = headers or {"Content-Type": "text/html; charset=utf-8"}


class FixtureClient:
    """Replays canned bodies, records every request, and fails on unknown URLs."""

    def __init__(self, mapping: dict[str, bytes] | None = None, *, end_status: int | None = None):
        self.mapping = dict(mapping or {})
        self.calls: list[str] = []
        # When set, unknown URLs answer with this HTTP status instead of raising.
        # CTFtime ends its listing with 404, so ``end_status=404`` mirrors reality.
        self.end_status = end_status

    def add(self, url: str, content: bytes) -> None:
        self.mapping[url] = content

    async def get(self, url: str, headers: dict | None = None) -> FixtureResponse:
        self.calls.append(url)
        if url not in self.mapping:
            if self.end_status is not None:
                raise FetchError(
                    f"remote source returned HTTP {self.end_status}",
                    category="permanent",
                    status_code=self.end_status,
                )
            raise AssertionError(f"unexpected request: {url}")
        return FixtureResponse(self.mapping[url], url)


def fixture_bytes(*parts: str) -> bytes:
    return FIXTURES.joinpath(*parts).read_bytes()


def make_config(adapter: str, *, max_items: int | None = None, **kwargs) -> SourceConfig:
    options = kwargs.pop("options", {})
    return SourceConfig(adapter=adapter, max_items=max_items, options=options, **kwargs)
