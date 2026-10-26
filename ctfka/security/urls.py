from __future__ import annotations

import asyncio
import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urlsplit


class URLBlocked(ValueError):
    """A target is outside the public HTTP(S) boundary."""


@dataclass(frozen=True, slots=True)
class PublicURL:
    url: str
    host: str
    port: int
    addresses: tuple[str, ...]


async def validate_public_url(url: str, *, resolve: bool = True) -> PublicURL:
    """Validate a URL and, by default, resolve every address before connecting.

    Redirects must be validated independently. Rejecting mixed public/private DNS
    answers prevents a hostname from smuggling a connection to a local service.
    """
    try:
        parsed = urlsplit(url)
        scheme = parsed.scheme.lower()
        host = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise URLBlocked(f"invalid URL: {exc}") from exc
    if scheme not in {"http", "https"} or not host:
        raise URLBlocked("only absolute public HTTP(S) URLs are allowed")
    if parsed.username is not None or parsed.password is not None:
        raise URLBlocked("credentials in URLs are forbidden")
    if port is None:
        port = 443 if scheme == "https" else 80
    if not 1 <= port <= 65535:
        raise URLBlocked("invalid URL port")
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if not literal.is_global:
            raise URLBlocked("non-public IP address")
        addresses = (str(literal),)
    elif resolve:
        try:
            records = await asyncio.to_thread(
                socket.getaddrinfo, host, port, type=socket.SOCK_STREAM
            )
        except OSError as exc:
            raise URLBlocked(f"DNS resolution failed for {host}: {exc}") from exc
        resolved = {record[4][0] for record in records}
        if not resolved:
            raise URLBlocked(f"DNS returned no addresses for {host}")
        for address in resolved:
            try:
                ip = ipaddress.ip_address(address.split("%", 1)[0])
            except ValueError as exc:
                raise URLBlocked(f"invalid DNS address for {host}") from exc
            if not ip.is_global:
                raise URLBlocked(f"{host} resolves to a non-public address")
        addresses = tuple(sorted(resolved))
    else:
        # Callers opting out of DNS are responsible for transport restrictions.
        addresses = ()
    return PublicURL(url=url, host=host, port=port, addresses=addresses)
