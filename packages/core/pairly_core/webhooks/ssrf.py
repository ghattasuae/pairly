# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""SSRF protection for webhook endpoint URLs (brief 6.5).

A tenant chooses where Pairly sends HTTP requests, so the URL is untrusted:

* Scheme ``https`` only (``http`` only with ``PAIRLY_WEBHOOK_ALLOW_HTTP`` in
  development). No user name or password in the URL, no fragment, a real host name.
* Host names that are local by definition (``localhost``, ``*.localhost``, ``*.local``,
  ``*.internal``, ``*.home.arpa``) are refused outright.
* The host is resolved and EVERY address must be public. Refused: private, loopback,
  link-local (this includes 169.254.169.254, the cloud metadata address), carrier-grade
  NAT (100.64.0.0/10, also Alibaba's metadata 100.100.100.200), multicast, reserved,
  documentation and benchmark ranges, unspecified addresses; for IPv6 also unique local
  (fc00::/7, so fd00:ec2::254), link-local, multicast, and IPv4 embedded in IPv6
  (IPv4-mapped, 6to4, NAT64 64:ff9b::/96, Teredo: the embedded IPv4 is checked or the
  address is refused). ``ipaddress.is_global`` is the base test.
* The check runs when the endpoint is saved AND again at every delivery, and the
  delivery connects to the exact address that passed the check (the URL host is
  replaced by that IP; ``Host`` and TLS SNI keep the name, and the certificate is still
  verified against the name). A name that resolves to a public address at save time
  and to an internal one later (DNS rebinding) is therefore refused at delivery.
* The sender never follows redirects, has short timeouts, ignores proxy environment
  variables and reads at most a small part of the response.

``PAIRLY_WEBHOOK_ALLOW_PRIVATE_TARGETS`` (development only) allows private addresses for
local testing; it can never be set in production (``WebhookSettings``).
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Final
from urllib.parse import urlsplit

from pairly_core.errors import BadRequestError

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
Resolver = Callable[[str, int], Awaitable[list[str]]]

MAX_URL_LENGTH: Final = 2048
_LOCAL_SUFFIXES: Final = (".localhost", ".local", ".internal", ".home.arpa", ".localdomain")
_LOCAL_NAMES: Final = frozenset({"localhost", "localhost.localdomain", "ip6-localhost"})
_NAT64: Final = ipaddress.ip_network("64:ff9b::/96")
_BLOCKED_NETWORKS: Final = tuple(
    ipaddress.ip_network(n)
    for n in (
        "0.0.0.0/8",
        "10.0.0.0/8",
        "100.64.0.0/10",
        "127.0.0.0/8",
        "169.254.0.0/16",
        "172.16.0.0/12",
        "192.0.0.0/24",
        "192.0.2.0/24",
        "192.88.99.0/24",
        "192.168.0.0/16",
        "198.18.0.0/15",
        "198.51.100.0/24",
        "203.0.113.0/24",
        "224.0.0.0/4",
        "240.0.0.0/4",
        "::/128",
        "::1/128",
        "::/96",
        "64:ff9b:1::/48",
        "100::/64",
        "2001:db8::/32",
        "fc00::/7",
        "fe80::/10",
        "fec0::/10",
        "ff00::/8",
    )
)


class UnsafeWebhookUrlError(BadRequestError):
    code = "unsafe_webhook_url"
    title = "This webhook URL is not allowed"


@dataclass(frozen=True, slots=True)
class TargetUrl:
    scheme: str
    host: str  # lower-case name or IP literal (no brackets)
    port: int
    path: str  # path plus query, at least "/"

    @property
    def host_header(self) -> str:
        default = 443 if self.scheme == "https" else 80
        host = f"[{self.host}]" if ":" in self.host else self.host
        return host if self.port == default else f"{host}:{self.port}"

    def url_for(self, ip: IPAddress) -> str:
        host = f"[{ip}]" if ip.version == 6 else str(ip)
        return f"{self.scheme}://{host}:{self.port}{self.path}"


def is_blocked_ip(ip: IPAddress) -> bool:
    """True for any address a webhook must never reach (see the module docstring)."""
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return is_blocked_ip(ip.ipv4_mapped)
        if ip.sixtofour is not None:
            return is_blocked_ip(ip.sixtofour)
        if ip.teredo is not None:
            return True
        if ip in _NAT64:
            return is_blocked_ip(ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF))
    if any(ip in network for network in _BLOCKED_NETWORKS):
        return True
    return (
        not ip.is_global
        or ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def parse_target(url: str, *, allow_http: bool = False) -> TargetUrl:
    """Validate the shape of an endpoint URL (no DNS). Raises ``UnsafeWebhookUrlError``."""
    if not isinstance(url, str) or not 8 <= len(url) <= MAX_URL_LENGTH or url != url.strip():
        raise UnsafeWebhookUrlError("the URL must be an absolute https URL")
    if any(ord(c) < 0x21 or ord(c) == 0x7F for c in url):
        raise UnsafeWebhookUrlError("the URL contains spaces or control characters")
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        raise UnsafeWebhookUrlError("the URL is malformed") from None
    scheme = parts.scheme.lower()
    if scheme != "https" and not (allow_http and scheme == "http"):
        raise UnsafeWebhookUrlError("webhook URLs must use https")
    if parts.username is not None or parts.password is not None:
        raise UnsafeWebhookUrlError("the URL must not contain credentials")
    if parts.fragment:
        raise UnsafeWebhookUrlError("the URL must not contain a fragment")
    host = (parts.hostname or "").rstrip(".").lower()
    if not host:
        raise UnsafeWebhookUrlError("the URL needs a host")
    if host in _LOCAL_NAMES or host.endswith(_LOCAL_SUFFIXES):
        raise UnsafeWebhookUrlError("the URL points to a local host")
    if port is None:
        port = 443 if scheme == "https" else 80
    if not 1 <= port <= 65535:
        raise UnsafeWebhookUrlError("the URL has an invalid port")
    path = parts.path or "/"
    if parts.query:
        path = f"{path}?{parts.query}"
    return TargetUrl(scheme=scheme, host=host, port=port, path=path)


async def system_resolver(host: str, port: int) -> list[str]:
    """Every address ``host`` resolves to (A and AAAA), via the system resolver."""
    infos = await asyncio.get_running_loop().getaddrinfo(
        host, port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP
    )
    return sorted({str(info[4][0]) for info in infos})


async def resolve_target(
    target: TargetUrl, resolver: Resolver, *, allow_private: bool = False
) -> IPAddress:
    """The address to connect to, after checking that every resolved address is safe."""
    try:
        literal: IPAddress | None = ipaddress.ip_address(target.host)
    except ValueError:
        literal = None
    if literal is not None:
        addresses: list[IPAddress] = [literal]
    else:
        try:
            raw = await resolver(target.host, target.port)
        except (OSError, UnicodeError, ValueError):
            raise UnsafeWebhookUrlError("the host name does not resolve") from None
        try:
            addresses = [ipaddress.ip_address(a.split("%", 1)[0]) for a in raw]
        except ValueError:
            raise UnsafeWebhookUrlError("the host name resolves to an invalid address") from None
    if not addresses:
        raise UnsafeWebhookUrlError("the host name does not resolve")
    if not allow_private and any(is_blocked_ip(a) for a in addresses):
        # One internal answer is enough to refuse: a resolver could alternate answers.
        raise UnsafeWebhookUrlError("the URL resolves to a private or reserved address")
    return addresses[0]
