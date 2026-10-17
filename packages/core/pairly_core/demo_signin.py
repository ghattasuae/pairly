# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Public sign-in for the hosted demo: which seeded demo users a visitor may sign in as.

A public demo has no mailbox a visitor can read, so the web app's demo sign-in page
(``/<locale>/demo``) issues a magic link straight to the browser, for the seeded demo
users only. This module is the API's half: it says whether the demo sign-in is on and
which accounts it offers. Three locks, all needed (fail closed):

1. The deployment switch ``PAIRLY_DEMO_PUBLIC_SIGNIN=true`` (default off).
2. A non-production environment: ``PAIRLY_ENV`` is never ``production``.
3. The organisation ``PAIRLY_DEMO_ORG_SLUG`` (default ``demo``) is active and has turned
   on its ``demo.public_signin`` feature flag (default off; the hosted demo reset
   turns it on for the seeded organisation, as its admin, audited).

Accounts offered: the members of that organisation whose address is on
``DEMO_ACCOUNTS``, the people ``scripts/demo_seed.py`` creates for Hire, Pay and the
expense example. Never the organisation admin: an admin manages members, API keys,
webhooks and flags, which visitors must not. An address not on the list is never
offered, whatever its role, so the sign-in cannot reach anybody else's account.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Final

from pydantic import Field, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from pairly_core.config import ENV_PREFIX, ConfigError, Environment
from pairly_core.db import set_tenant
from pairly_core.models import Membership, Role, User

DEFAULT_ORG_SLUG: Final = "demo"

# (address local part template, label key) in the order the page shows them. ``{slug}``
# is the demo organisation's slug. Kept in step with the seeds: pairly_hire.seed,
# pairly_expense.seed and scripts/demo_pay.py (a test checks it).
DEMO_ACCOUNTS: Final[tuple[tuple[str, str], ...]] = (
    ("{slug}-hire-recruiter@example.test", "recruiter"),
    ("{slug}-hire-manager@example.test", "hiring_manager"),
    ("{slug}-hire-interviewer@example.test", "interviewer"),
    ("{slug}-pay-clerk@example.test", "ap_clerk"),
    ("{slug}-pay-approver@example.test", "pay_approver"),
    ("{slug}-pay-finance@example.test", "finance_controller"),
    ("{slug}-expense-submitter@example.test", "expense_submitter"),
    ("{slug}-expense-approver@example.test", "expense_approver"),
)


class DemoSigninSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix=ENV_PREFIX, case_sensitive=False, extra="ignore", frozen=True
    )

    env: Environment = Environment.PRODUCTION
    demo_public_signin: bool = False
    demo_org_slug: str = Field(default=DEFAULT_ORG_SLUG, pattern=r"^[a-z0-9][a-z0-9-]{1,62}$")

    @property
    def enabled(self) -> bool:
        return self.demo_public_signin and self.env is not Environment.PRODUCTION


def load_demo_signin_settings(**overrides: Any) -> DemoSigninSettings:
    try:
        return DemoSigninSettings(**overrides)
    except ValidationError as exc:
        names = sorted({f"{ENV_PREFIX}{str(e['loc'][0]).upper()}" for e in exc.errors()})
        raise ConfigError("invalid demo sign-in settings: " + ", ".join(names)) from None


def allowed_emails(slug: str) -> dict[str, str]:
    """Address (lower case) to label key, for the demo organisation ``slug``."""
    return {template.format(slug=slug): label for template, label in DEMO_ACCOUNTS}


@dataclass(frozen=True, slots=True)
class DemoAccount:
    email: str
    label: str
    role: str
    workflow: str | None


async def demo_accounts(session: AsyncSession, settings: DemoSigninSettings) -> list[DemoAccount]:
    """The accounts the public demo sign-in offers, or ``[]`` when it is off.

    ``session`` must have no tenant and no open transaction: the lookup runs in its own
    transaction (committed), then the session is bound to the demo organisation and its
    members are read under Row Level Security.
    """
    if not settings.enabled:
        return []
    org_id = await session.scalar(
        text("SELECT demo_signin_org_id(:slug)"), {"slug": settings.demo_org_slug}
    )
    await session.commit()
    if not isinstance(org_id, uuid.UUID):
        return []
    allowed = allowed_emails(settings.demo_org_slug)
    set_tenant(session, org_id, None)
    async with session.begin():
        rows = (
            await session.execute(
                select(User.email, Role.key, Membership.workflow)
                .join(Membership, Membership.user_id == User.id)
                .join(Role, Role.id == Membership.role_id)
                .where(Membership.org_id == org_id, func.lower(User.email).in_(list(allowed)))
            )
        ).all()
    order = list(allowed)
    found = [
        DemoAccount(email.lower(), allowed[email.lower()], role, workflow)
        for email, role, workflow in rows
    ]
    return sorted(found, key=lambda a: (order.index(a.email), a.role, a.workflow or ""))
