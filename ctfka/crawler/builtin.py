"""Compatibility re-exports for built-in adapters."""

from ctfka.crawler.sources.ctfwiki import CTFWikiAdapter
from ctfka.crawler.sources.github import GitHubAdapter
from ctfka.crawler.sources.html import HTMLAdapter
from ctfka.crawler.sources.rss import RSSAdapter

__all__ = ["CTFWikiAdapter", "GitHubAdapter", "HTMLAdapter", "RSSAdapter"]
