"""Pluggable asynchronous public-source crawling."""

from .adapters import SourceAdapter
from .http import FetchClient, FetchError
from .registry import AdapterRegistry

__all__ = ["AdapterRegistry", "FetchClient", "FetchError", "SourceAdapter"]
