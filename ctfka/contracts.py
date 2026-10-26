from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class DiscoveredItem:
    url: str
    identity: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class FetchedDocument:
    url: str
    raw: bytes
    content_type: str = "text/markdown"
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ParsedDocument:
    title: str
    content: str
    metadata: dict[str, Any] = field(default_factory=dict)
