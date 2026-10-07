# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""``SlackTransport``: the Web API calls we make, behind an interface.

* ``HttpSlackTransport``: ``httpx`` against ``https://slack.com/api`` (``chat.postMessage``)
  and Slack's ``response_url`` for interaction replies. A ``response_url`` must be an
  ``https://hooks.slack.com/`` URL: anything else is refused, so a forged payload can
  never make us call an arbitrary host (SSRF).
* ``FakeSlackTransport``: records calls. Tests and local runs; no network.

Errors are ``DeliveryError``: rate limits and 5xx are transient, the rest permanent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import urlsplit

import httpx

from pairly_core.notifications.email import DeliveryError

RESPONSE_URL_HOST = "hooks.slack.com"
_TRANSIENT_ERRORS = frozenset(
    {"ratelimited", "service_unavailable", "fatal_error", "internal_error"}
)


def is_response_url(url: str) -> bool:
    parts = urlsplit(url)
    return parts.scheme == "https" and parts.hostname == RESPONSE_URL_HOST and parts.port is None


class SlackTransport(Protocol):
    async def post_message(
        self, *, token: str, channel: str, text: str, blocks: list[dict[str, Any]]
    ) -> str:
        """Post a message (a user id as channel opens the bot's DM). Returns its ``ts``."""
        ...

    async def respond(self, response_url: str, body: dict[str, Any]) -> None:
        """Reply to an interaction through its ``response_url``."""
        ...


class HttpSlackTransport:
    def __init__(
        self,
        *,
        base_url: str = "https://slack.com/api",
        client: httpx.AsyncClient | None = None,
        timeout: float = 10.0,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=timeout, follow_redirects=False)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def post_message(
        self, *, token: str, channel: str, text: str, blocks: list[dict[str, Any]]
    ) -> str:
        try:
            response = await self._client.post(
                f"{self._base}/chat.postMessage",
                json={"channel": channel, "text": text[:3000], "blocks": blocks},
                headers={"Authorization": f"Bearer {token}"},
            )
        except httpx.HTTPError:
            raise DeliveryError("slack_unavailable", transient=True) from None
        if response.status_code == 429 or response.status_code >= 500:
            raise DeliveryError(f"slack_{response.status_code}", transient=True)
        try:
            data = response.json()
        except ValueError:
            raise DeliveryError("slack_bad_response", transient=True) from None
        if not isinstance(data, dict) or data.get("ok") is not True:
            error = str(data.get("error", "unknown")) if isinstance(data, dict) else "unknown"
            raise DeliveryError(f"slack_{error}"[:64], transient=error in _TRANSIENT_ERRORS)
        return str(data.get("ts", ""))

    async def respond(self, response_url: str, body: dict[str, Any]) -> None:
        if not is_response_url(response_url):
            raise DeliveryError("slack_response_url_refused", transient=False)
        try:
            response = await self._client.post(response_url, json=body)
        except httpx.HTTPError:
            raise DeliveryError("slack_unavailable", transient=True) from None
        if response.status_code >= 400:
            raise DeliveryError(
                f"slack_{response.status_code}", transient=response.status_code >= 500
            )


@dataclass
class FakeSlackTransport:
    posts: list[dict[str, Any]] = field(default_factory=list)
    responses: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    fail_next: int = 0

    async def post_message(
        self, *, token: str, channel: str, text: str, blocks: list[dict[str, Any]]
    ) -> str:
        if self.fail_next > 0:
            self.fail_next -= 1
            raise DeliveryError("slack_ratelimited", transient=True)
        self.posts.append({"token": token, "channel": channel, "text": text, "blocks": blocks})
        return f"1700000000.{len(self.posts):06d}"

    async def respond(self, response_url: str, body: dict[str, Any]) -> None:
        if not is_response_url(response_url):
            raise DeliveryError("slack_response_url_refused", transient=False)
        self.responses.append((response_url, body))
