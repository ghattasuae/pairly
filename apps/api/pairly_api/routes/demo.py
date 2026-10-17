# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Hosted demo: ``GET /v1/demo/signin-accounts``, no sign-in.

The accounts a visitor may sign in as on the hosted demo (``pairly_core.demo_signin``):
the web app's demo sign-in page lists them and issues a magic link for one of them only
after checking it is on this list. Off unless ``PAIRLY_DEMO_PUBLIC_SIGNIN=true``, outside
production, and the demo organisation has turned its ``demo.public_signin`` flag on.
Off, or with no such organisation, the answer is the same 404 as any unknown path.

Read only, ``Cache-Control: no-store``, 120 requests a minute per connecting address
(shared through Valkey, with this process's window as the fallback). Behind a proxy that
address is the proxy's, so the limit is then one for all visitors together.
"""

from __future__ import annotations

import time
from collections import OrderedDict, deque
from typing import Final

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel

from pairly_core.demo_signin import DemoSigninSettings, demo_accounts, load_demo_signin_settings
from pairly_core.errors import NotFoundError, PairlyError
from pairly_core.identity import RequestSession
from pairly_core.ratelimit import window_limiters

router = APIRouter(prefix="/demo", tags=["demo"])

# (requests, seconds) per client address.
LIMITS: Final = {"accounts": (120, 60.0)}
MAX_KEYS: Final = 10_000


class DemoAccountOut(BaseModel):
    email: str
    label: str
    role: str
    workflow: str | None


class DemoAccountsOut(BaseModel):
    org_slug: str
    accounts: list[DemoAccountOut]


class DemoRateLimitedError(PairlyError):
    status = 429
    code = "rate_limited"
    title = "Too many requests"

    def __init__(self, retry_after: int) -> None:
        super().__init__("too many requests, try again later")
        self.retry_after = retry_after
        self.headers = {"Retry-After": str(retry_after)}


class _Window:
    """At most ``limit`` hits per key in any ``window`` seconds, in this process."""

    def __init__(self, limit: int, window: float) -> None:
        self.limit = limit
        self.window = window
        self._hits: OrderedDict[str, deque[float]] = OrderedDict()

    def hit(self, key: str) -> None:
        now = time.monotonic()
        hits = self._hits.setdefault(key, deque())
        self._hits.move_to_end(key)
        while hits and now - hits[0] >= self.window:
            hits.popleft()
        if len(hits) >= self.limit:
            raise DemoRateLimitedError(max(1, int(self.window - (now - hits[0])) + 1))
        hits.append(now)
        while len(self._hits) > MAX_KEYS:
            self._hits.popitem(last=False)


def _settings(request: Request) -> DemoSigninSettings:
    """``app.state.demo_signin`` (tests), else the environment."""
    found = getattr(request.app.state, "demo_signin", None)
    return found if isinstance(found, DemoSigninSettings) else load_demo_signin_settings()


@router.get("/signin-accounts", response_model=DemoAccountsOut)
async def signin_accounts(
    request: Request, response: Response, session: RequestSession
) -> DemoAccountsOut:
    settings = _settings(request)
    if not settings.enabled:
        raise NotFoundError("not found")
    peer = request.client.host if request.client else "unknown"
    await window_limiters(
        request, "demo_limiters", LIMITS, "demo", local=_Window, error=DemoRateLimitedError
    )["accounts"].hit(peer)
    accounts = await demo_accounts(session, settings)
    if not accounts:
        raise NotFoundError("not found")
    response.headers["Cache-Control"] = "no-store"
    return DemoAccountsOut(
        org_slug=settings.demo_org_slug,
        accounts=[
            DemoAccountOut(email=a.email, label=a.label, role=a.role, workflow=a.workflow)
            for a in accounts
        ],
    )
