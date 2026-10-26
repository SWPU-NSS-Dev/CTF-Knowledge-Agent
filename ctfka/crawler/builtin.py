"""Compatibility re-exports for built-in adapters."""

from ctfka.crawler.sources.anquanke import AnquankeAdapter
from ctfka.crawler.sources.ctftime import CTFtimeAdapter
from ctfka.crawler.sources.ctfwiki import CTFWikiAdapter
from ctfka.crawler.sources.exploitdb import ExploitDBAdapter
from ctfka.crawler.sources.github import GitHubAdapter
from ctfka.crawler.sources.hacktricks import HackTricksAdapter
from ctfka.crawler.sources.html import HTMLAdapter
from ctfka.crawler.sources.kanxue import KanxueAdapter
from ctfka.crawler.sources.rss import RSSAdapter
from ctfka.crawler.sources.sitemap import SitemapAdapter

__all__ = [
    "AnquankeAdapter",
    "CTFWikiAdapter",
    "CTFtimeAdapter",
    "ExploitDBAdapter",
    "GitHubAdapter",
    "HTMLAdapter",
    "HackTricksAdapter",
    "KanxueAdapter",
    "RSSAdapter",
    "SitemapAdapter",
]
