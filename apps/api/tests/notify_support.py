# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Support for the notification, Slack and receipt database tests (WP1.9).

* ``notify_settings``: notification settings for tests (fake email, fixed secrets, fast
  server-sent events). No network is ever used: email goes to ``FakeEmailTransport``
  and Slack to ``FakeSlackTransport``.
* ``NotifyApi`` / ``notify_api``: the API with those settings, the approval
  notification hook switched on, and a fake Slack transport.
* ``hooked_runtime``: a workflow runtime (test workflow from ``wf_support``) whose
  approvals send notifications.
* ``notifications``: read notification rows of one organisation, with their decrypted
  content.
"""

from __future__ import annotations

import dataclasses
import uuid
from collections.abc import AsyncIterator, Mapping
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from pairly_api.main import create_app
from pairly_core.approvals.hooks import ApprovalHooks
from pairly_core.config import Environment
from pairly_core.crypto.types import unseal
from pairly_core.db import create_sessionmaker, set_tenant
from pairly_core.integrations.slack.transport import FakeSlackTransport
from pairly_core.notifications.approval_hook import NotificationApprovalHook
from pairly_core.notifications.models import Notification
from pairly_core.notifications.settings import NotificationSettings
from pairly_core.workflow.checkpoint import TenantCheckpointer
from pairly_core.workflow.runtime import WorkflowRuntime

from .conftest import DbUrls, TokenFactory, make_settings
from .wf_support import PLUGINS, REGISTRY, SPEC, World

WEB_URL = "https://app.test.pairly"
API_URL = "https://api.test.pairly"
LINK_SECRET = "unsubscribe-secret-for-tests-" + "x" * 16
SLACK_SECRET = "slack-signing-secret-for-tests-0123456789"  # noqa: S105 (test value)


def notify_settings(**overrides: Any) -> NotificationSettings:
    values: dict[str, Any] = {
        "env": Environment.TEST,
        "public_web_url": WEB_URL,
        "public_api_url": API_URL,
        "email_transport": "fake",
        "smtp_from": "Pairly <no-reply@pairly.localhost>",
        "signed_link_secret": SecretStr(LINK_SECRET),
        "slack_signing_secret": SecretStr(SLACK_SECRET),
        "sse_poll_seconds": 0.05,
        "sse_max_seconds": 1,
    }
    values.update(overrides)
    return NotificationSettings(**values)


def approval_hooks(settings: NotificationSettings) -> ApprovalHooks:
    return ApprovalHooks([NotificationApprovalHook(settings)])


def hooked_runtime(
    engine: AsyncEngine, migrated: DbUrls, settings: NotificationSettings
) -> WorkflowRuntime:
    return WorkflowRuntime(
        sessionmaker=create_sessionmaker(engine),
        checkpointer=TenantCheckpointer(migrated.app),
        registry=REGISTRY,
        graphs=[SPEC],
        hooks=approval_hooks(settings),
    )


@dataclasses.dataclass
class NotifyApi:
    app: FastAPI
    client: httpx.AsyncClient
    tokens: TokenFactory
    settings: NotificationSettings
    slack: FakeSlackTransport

    @property
    def runtime(self) -> WorkflowRuntime:
        runtime = self.app.state.workflow_runtime
        assert isinstance(runtime, WorkflowRuntime)
        return runtime

    @property
    def sessionmaker(self) -> async_sessionmaker[AsyncSession]:
        return self.runtime.sessionmaker

    def headers(
        self, world: World, name: str, *, otp: bool = True, org: uuid.UUID | None = None
    ) -> dict[str, str]:
        claims: dict[str, Any] = {"amr": ["otp"]} if otp else {}
        token = self.tokens.token(world.people[name].subject, **claims)
        return {
            "Authorization": f"Bearer {token}",
            "X-Pairly-Org": str(org or world.org_id),
        }


@dataclasses.dataclass
class AppFactory:
    migrated: DbUrls
    tokens: TokenFactory

    async def __call__(self, settings: NotificationSettings) -> AsyncIterator[NotifyApi]:
        slack = FakeSlackTransport()
        app = create_app(
            make_settings(self.migrated.app),
            plugins=PLUGINS,
            approval_hooks=approval_hooks(settings),
            notification_settings=settings,
            slack_transport=slack,
        )
        async with app.router.lifespan_context(app):
            app.state.pairly = dataclasses.replace(
                app.state.pairly, verifier=self.tokens.verifier()
            )
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://api.test") as c:
                yield NotifyApi(app, c, self.tokens, settings, slack)


@pytest.fixture
async def notify_api(migrated: DbUrls, tokens: TokenFactory) -> AsyncIterator[NotifyApi]:
    async for api in AppFactory(migrated, tokens)(notify_settings()):
        yield api


async def notifications(
    sessionmaker: async_sessionmaker[AsyncSession],
    org_id: uuid.UUID,
    **where: Any,
) -> list[tuple[Notification, dict[str, Any]]]:
    """The organisation's notification rows matching ``where`` (column equality), oldest
    first, each with its decrypted content (an empty dict when suppressed)."""
    async with sessionmaker() as session:
        set_tenant(session, org_id, None)
        async with session.begin():
            query = select(Notification).order_by(Notification.created_at, Notification.id)
            for column, value in where.items():
                query = query.where(getattr(Notification, column) == value)
            rows = list(await session.scalars(query))
            return [(row, dict(unseal(row, "content") or {})) for row in rows]


def link_token(url: str) -> str:
    """The signed link token of a web confirmation URL (``.../approvals/link#<token>``)."""
    assert url.startswith(f"{WEB_URL}/approvals/link#"), url
    return url.rsplit("#", 1)[1]


def text_of(content: Mapping[str, Any]) -> str:
    return " ".join(str(content.get(k) or "") for k in ("subject", "text", "html"))
