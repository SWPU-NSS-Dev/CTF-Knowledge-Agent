from __future__ import annotations

import importlib
from importlib.metadata import entry_points
from typing import Any

from ctfka.config import SourceConfig
from ctfka.crawler.adapters import SourceAdapter
from ctfka.crawler.http import FetchClient

BUILTINS = {
    "ctfwiki": "ctfka.crawler.sources.ctfwiki:CTFWikiAdapter",
    "github": "ctfka.crawler.sources.github:GitHubAdapter",
    "html": "ctfka.crawler.sources.html:HTMLAdapter",
    "rss": "ctfka.crawler.sources.rss:RSSAdapter",
}


def _load(spec: str) -> Any:
    module_name, sep, attribute = spec.partition(":")
    if not sep or not module_name or not attribute:
        raise ValueError("adapter must be registered as module:Class")
    module = importlib.import_module(module_name)
    return getattr(module, attribute)


class AdapterRegistry:
    """Discovers built-ins, installed entry points and explicit trusted classes."""

    def __init__(self) -> None:
        self._classes: dict[str, Any] = dict(BUILTINS)
        try:
            for ep in entry_points(group="ctfka.sources"):
                self._classes[ep.name] = ep.value
        except TypeError:  # Python metadata API on older supported runtimes.
            for ep in entry_points().get("ctfka.sources", []):
                self._classes[ep.name] = ep.value

    def names(self) -> list[str]:
        return sorted(self._classes)

    def create(self, name: str, config: SourceConfig, client: FetchClient) -> SourceAdapter:
        spec = self._classes.get(name, name if ":" in name else None)
        if spec is None:
            raise KeyError(f"unknown source adapter: {name}")
        cls = _load(spec) if isinstance(spec, str) else spec.load()
        adapter = cls(config, client)
        if not isinstance(adapter, SourceAdapter):
            raise TypeError(f"adapter {name!r} must implement SourceAdapter")
        return adapter
