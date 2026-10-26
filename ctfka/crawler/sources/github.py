from __future__ import annotations

import os
from collections.abc import AsyncIterator
from urllib.parse import quote

from ctfka.contracts import DiscoveredItem, FetchedDocument
from ctfka.crawler.adapters import SourceAdapter
from ctfka.crawler.http import FetchError


class GitHubAdapter(SourceAdapter):
    """Incremental scanner for public repository Markdown and HTML files."""

    api_root = "https://api.github.com"

    def _repo(self) -> tuple[str, str, str]:
        owner = str(self.options.get("owner", ""))
        repo = str(self.options.get("repo", ""))
        ref = str(self.options.get("ref", "HEAD"))
        if not owner or not repo:
            raise ValueError("github adapter requires options.owner and options.repo")
        return owner, repo, ref

    async def discover(self, cursor=None) -> AsyncIterator[DiscoveredItem]:
        owner, repo, ref = self._repo()
        previous = dict(cursor or {})
        self._complete_scan = False
        self._candidate_commit = None
        self._current_identities: list[str] = []
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        github_token = os.getenv("GITHUB_TOKEN")
        if github_token:
            headers["Authorization"] = f"Bearer {github_token}"
        if ref == "HEAD":
            repo_res = await self.client.get(
                f"{self.api_root}/repos/{quote(owner)}/{quote(repo)}", headers
            )
            ref = str(repo_res.json().get("default_branch", "main"))
        commit_url = (
            f"{self.api_root}/repos/{quote(owner)}/{quote(repo)}/commits/{quote(ref, safe='')}"
        )
        conditional_headers = dict(headers)
        etag = previous.get("commit_etag")
        if previous.get("commit") and isinstance(etag, str) and etag:
            conditional_headers["If-None-Match"] = etag
        commit_res = await self.client.get(commit_url, conditional_headers)
        if getattr(commit_res, "status_code", None) == 304:
            if previous.get("commit") and previous.get("files"):
                self._state = previous
                return
            # A conditional response without a locally committed tree cannot safely
            # advance state. Retry once without the validator before giving up.
            commit_res = await self.client.get(commit_url, headers)
            if getattr(commit_res, "status_code", None) == 304:
                raise FetchError(
                    "GitHub returned 304 without a committed local tree",
                    category="permanent",
                    status_code=304,
                )
        commit = commit_res.json().get("sha")
        if not isinstance(commit, str):
            raise FetchError("GitHub commit response has no sha", category="permanent")
        self._candidate_commit = commit
        if previous.get("commit") == commit:
            self._state = previous
            return
        root_tree = commit_res.json().get("commit", {}).get("tree", {}).get("sha")
        if not root_tree:
            raise ValueError("GitHub commit response has no root tree")
        file_blobs: dict[str, str] = {}
        stack = [("", root_tree)]
        total_files = 0
        limit = self.config.max_items
        while stack:
            prefix, tree_sha = stack.pop()
            tree_res = await self.client.get(
                f"{self.api_root}/repos/{quote(owner)}/{quote(repo)}/git/trees/{quote(tree_sha)}",
                headers,
            )
            data = tree_res.json()
            if data.get("truncated"):
                raise ValueError(
                    f"GitHub tree {tree_sha} was truncated; refusing to archive or advance an incomplete scan"
                )
            for node in data.get("tree", []):
                node_path = node.get("path")
                if not isinstance(node_path, str):
                    continue
                path = f"{prefix}/{node_path}" if prefix else node_path
                if node.get("type") == "tree":
                    stack.append((path, node["sha"]))
                elif node.get("type") == "blob":
                    if not path.lower().endswith((".md", ".markdown", ".html", ".htm")):
                        continue
                    total_files += 1
                    # A truncated recursive tree is handled naturally by the non-recursive walk.
                    blob_sha = str(node.get("sha", ""))
                    file_blobs[path] = blob_sha
                    identity = f"github:{owner}/{repo}:{path}"
                    self._current_identities.append(identity)
                    if previous.get("files", {}).get(path) == blob_sha:
                        continue
                    raw_url = (
                        f"https://raw.githubusercontent.com/{quote(owner)}/{quote(repo)}/{commit}/"
                        + quote(path, safe="/")
                    )
                    page_url = (
                        f"https://github.com/{quote(owner)}/{quote(repo)}/blob/{commit}/"
                        + quote(path, safe="/")
                    )
                    yield DiscoveredItem(
                        url=page_url,
                        identity=identity,
                        metadata={
                            "raw_url": raw_url,
                            "owner": owner,
                            "repo": repo,
                            "path": path,
                            "commit": commit,
                            "blob_sha": blob_sha,
                        },
                    )
                    if limit is not None and total_files >= limit:
                        # A limited scan is not complete and cannot advance the commit cursor.
                        self._candidate_commit = None
                        return
        response_headers = getattr(commit_res, "headers", {})
        response_etag = response_headers.get("ETag") if response_headers else None
        self._state = {"commit": commit, "files": file_blobs}
        if isinstance(response_etag, str) and response_etag:
            self._state["commit_etag"] = response_etag
        self._complete_scan = True

    async def fetch(self, item: DiscoveredItem) -> FetchedDocument:
        raw_url = item.metadata.get("raw_url")
        if not raw_url:
            raise ValueError("GitHub item is missing raw_url")
        response = await self.client.get(str(raw_url))
        suffix = str(item.metadata.get("path", "")).lower().rsplit(".", 1)[-1]
        content_type = "text/html" if suffix in {"html", "htm"} else "text/markdown"
        metadata = dict(item.metadata)
        metadata["fetched_commit"] = metadata.get("commit")
        return FetchedDocument(item.url, response.content, content_type, metadata)

    @property
    def complete_scan(self) -> bool:
        return bool(getattr(self, "_complete_scan", False))

    @property
    def candidate_commit(self) -> str | None:
        return getattr(self, "_candidate_commit", None)

    @property
    def current_identities(self) -> list[str]:
        return list(getattr(self, "_current_identities", []))
