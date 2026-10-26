from __future__ import annotations

from urllib.parse import urldefrag


def _as_urls(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return value
    return []


def _canonical(url: str) -> str:
    return urldefrag(url)[0]
