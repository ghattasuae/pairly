# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""``pairly-admin sandbox create``: a sandbox organisation with demo data and a test key.

    uv run pairly-admin sandbox create --name "Acme sandbox" [--slug sandbox-acme] \
        [--admin-email you@acme.test] [--days 30]

What it makes, all synthetic, in one organisation flagged ``sandbox``:

1. the organisation (``pairly_core.invitations.bootstrap_organisation``) with
   ``organisations.sandbox = true``, so it only ever has ``pk_test_`` keys and those keys
   work nowhere else (``pairly_core.api_keys.service``);
2. a sandbox owner, a service person who never signs in (``sandbox:<org id>``, an
   ``.invalid`` address): admin, plus approver for Pay. API keys need a person who
   created them, and a key with ``core.approval.read`` reads its creator's inbox, so
   the MCP server's "which approvals are waiting for me?" has an answer;
3. two pending Pay invoice approvals for that owner, one overdue, one with a decision
   record (``pairly_api.devseed.seed_approval``, the real approval service and rules);
4. a ``pk_test_`` key created by the owner with read, propose and upload scopes.

The key is printed once. With ``--admin-email`` the admin invitation link is printed too,
so a real person can sign in and look around (they are a separate member).
"""

from __future__ import annotations

import secrets
import uuid
from dataclasses import dataclass, field
from typing import Final

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import create_async_engine

from pairly_api.devseed import seed_approval
from pairly_core.api_keys.service import create_api_key
from pairly_core.api_keys.tokens import KeyMode
from pairly_core.audit import ActorType, AuditAction, append_audit
from pairly_core.config import to_asyncpg_url
from pairly_core.db import create_sessionmaker, set_tenant
from pairly_core.invitations import bootstrap_organisation
from pairly_core.models import Membership, Organisation, Role, User
from pairly_core.plugins import discover_plugins
from pairly_core.rbac import Actor, ActorKind, RoleGrant, api_key_may_hold, build_registry

SANDBOX_ACTOR: Final = "pairly-admin.sandbox"
# What a sandbox key may do (those the registry knows and the owner holds).
SANDBOX_SCOPES: Final = (
    "core.approval.read",
    "core.decision.read",
    "core.document.read",
    "core.document.upload",
    "core.proposal.propose",
    "core.proposal.read",
    "pay.invoice.read",
    "pay.invoice.upload",
    "pay.vendor.read",
    "hire.candidate.read",
    "hire.job.read",
)


@dataclass(frozen=True, slots=True)
class SandboxCreated:
    org_id: uuid.UUID
    slug: str
    owner_email: str
    key_prefix: str
    scopes: tuple[str, ...]
    approval_ids: tuple[str, ...]
    decision_id: str | None
    key: str = field(repr=False)
    invitation_token: str | None = field(default=None, repr=False)


def default_slug() -> str:
    return f"sandbox-{secrets.token_hex(3)}"


async def create_sandbox(
    owner_url: str,
    *,
    name: str,
    slug: str,
    admin_email: str | None = None,
    days: int = 30,
) -> SandboxCreated:
    plugins = list(discover_plugins().values())
    registry = build_registry(plugins)
    org_id = uuid.uuid4()
    subject = f"sandbox:{org_id}"
    owner_email = f"owner@{slug}.sandbox.pairly.invalid"
    engine = create_async_engine(to_asyncpg_url(owner_url), pool_size=1, max_overflow=0)
    sessionmaker = create_sessionmaker(engine)
    try:
        session = sessionmaker()
        async with session:
            booted = await bootstrap_organisation(
                session,
                name=name,
                slug=slug,
                admin_email=admin_email or owner_email,
                org_id=org_id,
            )
            await session.execute(
                update(Organisation).where(Organisation.id == org_id).values(sandbox=True)
            )
            await session.commit()

        # People rows are visible by subject (Row Level Security): a session of the owner.
        session = sessionmaker()
        set_tenant(session, org_id, subject)
        async with session:
            owner = User(id=uuid.uuid4(), auth_subject=subject, email=owner_email)
            owner.display_name = "Sandbox owner"
            session.add(owner)
            await session.flush()
            admin = await session.scalar(
                select(Role).where(Role.key == "admin", Role.org_id.is_(None))
            )
            if admin is None:  # pragma: no cover - seeded by migration 0001
                raise RuntimeError("the built-in admin role is missing")
            membership = Membership(org_id=org_id, user_id=owner.id, role_id=admin.id)
            session.add(membership)
            await session.flush()
            await append_audit(
                session,
                action=AuditAction.MEMBERSHIP_GRANTED,
                actor_type=ActorType.SYSTEM,
                actor_id=SANDBOX_ACTOR,
                entity_type="membership",
                entity_id=membership.id,
                payload={"user_id": str(owner.id), "role": "admin", "sandbox": True},
            )
            await session.commit()

        approvals: list[str] = []
        decision_id: str | None = None
        for title, overdue, decision in (
            ("Invoice INV-SBX-1001 from Sample Supplies LLC", False, True),
            ("Invoice INV-SBX-1002 from Gulf Office Trading", True, False),
        ):
            seeded = await seed_approval(
                owner_url,
                org_slug=slug,
                approver_email=owner_email,
                title=title,
                overdue=overdue,
                link=None,
                decision=decision,
            )
            approvals.append(str(seeded["approval_id"]))
            decision_id = decision_id or seeded["decision_id"]

        session = sessionmaker()
        set_tenant(session, org_id, subject)
        async with session, session.begin():
            rows = (
                await session.execute(
                    select(Role.key, Membership.workflow)
                    .join(Role, Role.id == Membership.role_id)
                    .where(Membership.user_id == owner.id, Membership.org_id == org_id)
                )
            ).all()
            grants = tuple(RoleGrant(role=r.key, workflow=r.workflow) for r in rows)
            actor = Actor(
                kind=ActorKind.USER,
                id=owner.id,
                org_id=org_id,
                grants=grants,
                permissions=registry.resolve(grants),
                subject=subject,
                email=owner_email,
            )
            scopes = tuple(
                s for s in SANDBOX_SCOPES if s in registry and actor.has(s) and api_key_may_hold(s)
            )
            created = await create_api_key(
                session,
                registry,
                actor,
                name="Sandbox key",
                scopes=scopes,
                mode=KeyMode.TEST,
                expires_in_days=days,
            )
        return SandboxCreated(
            org_id=org_id,
            slug=slug,
            owner_email=owner_email,
            key_prefix=created.key.prefix,
            scopes=scopes,
            approval_ids=tuple(approvals),
            decision_id=decision_id,
            key=created.token,
            invitation_token=booted.token if admin_email else None,
        )
    finally:
        await engine.dispose()
