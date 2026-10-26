from __future__ import annotations

import asyncio
import email.utils
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Self
from urllib.parse import urljoin, urlsplit

import httpx

from ctfka.config import SourceConfig
from ctfka.security import URLBlocked, validate_public_url


class FetchError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        category: str = "network",
        retry_after: float | None = None,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.category = category
        self.retry_after = retry_after
        self.status_code = status_code


class FetchClient:
    """Reusable, bounded HTTP client with per-origin robots and rate control."""

    def __init__(self, config: SourceConfig):
        self.config = config
        self._client: httpx.AsyncClient | None = None
        self._semaphore = asyncio.Semaphore(config.concurrency)
        self._rate_lock = asyncio.Lock()
        self._next_request_at = 0.0
        self._robots: dict[str, tuple[float, object]] = {}
        self._robots_locks: dict[str, asyncio.Lock] = {}

    async def __aenter__(self) -> Self:
        proxy = self.config.options.get("proxy_url")
        if proxy and urlsplit(str(proxy)).scheme.lower() not in {"socks5", "socks5h"}:
            raise ValueError(
                "proxy_url must use SOCKS5; HTTP CONNECT proxies cannot preserve pinned TLS host verification"
            )
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(self.config.timeout_seconds),
            limits=httpx.Limits(
                max_connections=self.config.concurrency,
                max_keepalive_connections=self.config.concurrency,
            ),
            follow_redirects=False,
            trust_env=False,
            proxy=str(proxy) if proxy else None,
            headers={"User-Agent": "CTF-Knowledge-Agent/1.0 (+local public-source indexer)"},
        )
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _pace(self) -> None:
        rate = self.config.requests_per_second
        if rate <= 0:
            return
        async with self._rate_lock:
            now = time.monotonic()
            delay = max(0.0, self._next_request_at - now)
            self._next_request_at = max(now, self._next_request_at) + 1.0 / rate
        if delay:
            await asyncio.sleep(delay)

    @staticmethod
    def _retry_after(value: str | None) -> float | None:
        if not value:
            return None
        try:
            return max(0.0, float(value))
        except ValueError:
            try:
                parsed: datetime = email.utils.parsedate_to_datetime(value)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=UTC)
                return max(0.0, (parsed - datetime.now(UTC)).total_seconds())
            except (TypeError, ValueError, OverflowError):
                return None

    async def _robots_allowed(self, url: str) -> bool:
        if self.config.options.get("respect_robots", True) is False:
            return True
        from urllib.parse import urlsplit
        from urllib.robotparser import RobotFileParser

        parsed = urlsplit(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        cached = self._robots.get(origin)
        now = time.monotonic()
        if cached and cached[0] > now:
            return cached[1].can_fetch("CTF-Knowledge-Agent", url)
        lock = self._robots_locks.setdefault(origin, asyncio.Lock())
        async with lock:
            cached = self._robots.get(origin)
            if cached and cached[0] > time.monotonic():
                return cached[1].can_fetch("CTF-Knowledge-Agent", url)
            robots_url = origin + "/robots.txt"
            parser = RobotFileParser(robots_url)
            try:
                response = await self._request_chain(
                    robots_url, {}, check_robots=False, allow_error_status=True
                )
                if response.status_code in {401, 403}:
                    parser.parse(["User-agent: *", "Disallow: /"])
                elif response.status_code == 404:
                    parser.parse(["User-agent: *", "Allow: /"])
                elif response.status_code >= 400:
                    # A transient robots failure must not silently authorize crawling.
                    raise FetchError(
                        f"robots.txt request returned {response.status_code}",
                        category="network",
                        status_code=response.status_code,
                        retry_after=self._retry_after(response.headers.get("Retry-After")),
                    )
                else:
                    parser.parse(response.text.splitlines())
            except httpx.HTTPError as exc:
                raise FetchError(f"robots.txt unavailable: {exc}") from exc
            self._robots[origin] = (time.monotonic() + 3600.0, parser)
            return parser.can_fetch("CTF-Knowledge-Agent", url)

    async def _request_chain(
        self,
        url: str,
        headers: Mapping[str, str],
        *,
        check_robots: bool,
        allow_error_status: bool = False,
    ) -> httpx.Response:
        if self._client is None:
            raise RuntimeError("FetchClient must be used as an async context manager")
        current = url
        visited: set[str] = set()
        # Bound redirects and validate each destination before any connection.
        for _ in range(11):
            if current in visited:
                raise FetchError("redirect loop", category="blocked")
            visited.add(current)
            try:
                await validate_public_url(current)
            except URLBlocked as exc:
                raise FetchError(str(exc), category="blocked") from exc
            if check_robots and not await self._robots_allowed(current):
                raise FetchError(f"robots.txt disallows {current}", category="blocked")
            await self._pace()
            try:
                target = await validate_public_url(current)
                if not target.addresses:
                    raise FetchError(
                        "URL validation returned no pinned public address", category="blocked"
                    )
                original_request = self._client.build_request("GET", current, headers=headers)
                pinned_url = httpx.URL(current).copy_with(host=target.addresses[0])
                request = self._client.build_request("GET", pinned_url, headers=headers)
                request.extensions["sni_hostname"] = target.host.encode("idna").decode("ascii")
                request.headers["Host"] = urlsplit(current).netloc
                async with self._semaphore:
                    response = await self._client.send(request, stream=True, follow_redirects=False)
                    response._request = original_request
                    try:
                        if response.status_code in {301, 302, 303, 307, 308}:
                            location = response.headers.get("Location")
                            if not location:
                                return response
                            current = urljoin(current, location)
                            continue
                        limit = self.config.max_response_bytes
                        chunks: list[bytes] = []
                        size = 0
                        async for chunk in response.aiter_bytes():
                            size += len(chunk)
                            if limit is not None and size > limit:
                                raise FetchError(
                                    f"response exceeds max_response_bytes ({limit})",
                                    category="blocked",
                                    status_code=response.status_code,
                                )
                            chunks.append(chunk)
                        response._content = b"".join(chunks)
                        response._stream = None
                        result = response
                    finally:
                        await response.aclose()
            except URLBlocked as exc:
                raise FetchError(str(exc), category="blocked") from exc
            except (httpx.TimeoutException, httpx.NetworkError, httpx.ProtocolError) as exc:
                raise FetchError(f"request failed: {exc}", category="network") from exc
            except httpx.HTTPError as exc:
                raise FetchError(f"request failed: {exc}", category="network") from exc
            if result.status_code == 429 and not allow_error_status:
                raise FetchError(
                    "remote source rate limited the request (429)",
                    category="rate_limited",
                    retry_after=self._retry_after(result.headers.get("Retry-After")),
                    status_code=429,
                )
            if result.status_code >= 400 and not (
                allow_error_status and result.status_code in {401, 403, 404}
            ):
                if result.status_code == 403 and result.headers.get("X-RateLimit-Remaining") == "0":
                    try:
                        retry_after = max(
                            0.0, float(result.headers.get("X-RateLimit-Reset", "0")) - time.time()
                        )
                    except ValueError:
                        retry_after = None
                    raise FetchError(
                        "GitHub API rate limit reached (403)",
                        category="rate_limited",
                        retry_after=retry_after,
                        status_code=403,
                    )
                category = (
                    "permanent" if result.status_code in {400, 401, 403, 404, 410} else "network"
                )
                raise FetchError(
                    f"remote source returned HTTP {result.status_code}",
                    category=category,
                    retry_after=self._retry_after(result.headers.get("Retry-After")),
                    status_code=result.status_code,
                )
            return result
        raise FetchError("too many redirects", category="blocked")

    async def get(self, url: str, headers: dict[str, str] | None = None) -> httpx.Response:
        return await self._request_chain(url, headers or {}, check_robots=True)
