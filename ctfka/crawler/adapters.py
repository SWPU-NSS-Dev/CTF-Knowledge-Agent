from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import Any

from ctfka.config import SourceConfig
from ctfka.contracts import DiscoveredItem, FetchedDocument, ParsedDocument
from ctfka.crawler.http import FetchClient


class SourceAdapter(ABC):
    """Site plugin contract. Implementations must not execute source content."""

    def __init__(self, config: SourceConfig, client: FetchClient):
        self.config = config
        self.client = client
        self.options: dict[str, Any] = config.options
        self._state: dict[str, Any] = {}

    @property
    def state(self) -> dict[str, Any]:
        return dict(self._state)

    def set_state(self, state: dict[str, Any] | None) -> None:
        self._state = dict(state or {})

    @abstractmethod
    async def discover(self, cursor: dict[str, Any] | None = None) -> AsyncIterator[DiscoveredItem]:
        """Yield a complete source scan; do not advance durable state here."""
        if False:
            yield DiscoveredItem("", "")

    @abstractmethod
    async def fetch(self, item: DiscoveredItem) -> FetchedDocument:
        raise NotImplementedError

    def parse(self, document: FetchedDocument) -> ParsedDocument:
        from ctfka.ingestion.parser import parse_document

        return parse_document(document)

    def normalize(self, parsed: ParsedDocument) -> ParsedDocument:
        parsed.title = " ".join(parsed.title.split())
        parsed.content = parsed.content.replace("\r\n", "\n").strip()
        return parsed

    def validate(self, parsed: ParsedDocument) -> None:
        if not parsed.title.strip() or not parsed.content.strip():
            raise ValueError("document has no title or readable content")
