# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Attempts from the October 2026 security review that need no database
(``docs/security-review-2026-10.md``)."""

from __future__ import annotations

import pytest

from pairly_core.webhooks.ssrf import (
    UnsafeWebhookUrlError,
    parse_target,
    resolve_target,
    system_resolver,
)


@pytest.mark.parametrize(
    "url",
    [
        "https://2130706433/",  # 127.0.0.1 as one decimal number
        "https://0x7f000001/",  # the same in hex
        "https://127.1/",  # short form
        "https://0/",  # 0.0.0.0
        "https://[::ffff:127.0.0.1]/",  # IPv4 mapped
        "https://[::ffff:a9fe:a9fe]/",  # the metadata address, mapped
        "https://[64:ff9b::a9fe:a9fe]/",  # the metadata address through NAT64
        "https://[fd00::1]/",  # unique local
        "https://[fe80::1]/",  # link local
    ],
)
async def test_numeric_host_spellings_of_internal_addresses_are_refused(url: str) -> None:
    """Webhook and batch URL SSRF: every numeric spelling the system resolver accepts
    (no DNS query is made for these) still lands on a blocked address."""
    target = parse_target(url)
    with pytest.raises(UnsafeWebhookUrlError):
        await resolve_target(target, system_resolver)


async def test_an_octal_looking_host_is_checked_as_the_address_it_resolves_to() -> None:
    """``0177.0.0.1`` is 127.0.0.1 to some resolvers and 177.0.0.1 to others. Whatever
    this one says, the request goes to the address that was checked (the URL is rewritten
    to it), so no second resolution can reach loopback."""
    target = parse_target("https://0177.0.0.1/x")
    ip = await resolve_target(target, system_resolver)
    assert not ip.is_loopback
    assert target.url_for(ip) == f"https://{ip}:443/x"
