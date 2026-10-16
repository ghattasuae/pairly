# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Inbound email addresses: format, parsing, lookup and token rotation.

Every organisation gets one address per workflow:

    <workflow>+<org-slug>.<token>@<PAIRLY_INBOUND_EMAIL_DOMAIN>
    for example pay+acme.k3m7q2xv7hb4tnwe@inbound.example.com

The organisation slug is public and guessable, so it is not enough on its own: the
token is a random 16 character base32 value (80 bits) generated per organisation and
workflow. Only its SHA-256 is stored (``inbound_addresses``), so the full address is
shown once, when the token is generated with ``pairly-admin inbound rotate``. Rotating
replaces the token; mail to the old address stops being accepted at once.

An address resolves through ``inbound_org_id(workflow, slug, token_hash)``, a narrow
SECURITY DEFINER function (migration 0004) that returns the organisation id only when
the slug, workflow and token all match an active organisation. Mail to an unknown
organisation, a wrong token or no token is dropped and logged, never bounced: a bounce
would confirm to the sender which organisations and addresses exist.
"""

from __future__ import annotations

import base64
import hashlib
import re
import secrets
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from email.utils import getaddresses
from typing import Final

from sqlalchemy import func, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from pairly_core.audit import append_audit
from pairly_core.db import TenantError, current_org
from pairly_core.ingestion.models import InboundAddress

TOKEN_BYTES: Final = 10  # 16 base32 characters, 80 bits
ROTATED_ACTION: Final = "core.inbound_address.rotated"
# How many candidate recipients one email may try (Delivered-To, To, Cc, ...).
MAX_ROUTE_CANDIDATES: Final = 5

WORKFLOW_RE: Final = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
SLUG_RE: Final = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")
TOKEN_RE: Final = re.compile(r"^[a-z2-7]{10,64}$")


@dataclass(frozen=True, slots=True)
class InboundRoute:
    workflow: str
    org_slug: str
    # Secret: kept out of repr so it never lands in logs or tracebacks.
    token: str = field(repr=False)


def new_inbound_token() -> str:
    """A fresh random token: 16 lowercase base32 characters (a-z, 2-7)."""
    return base64.b32encode(secrets.token_bytes(TOKEN_BYTES)).decode("ascii").rstrip("=").lower()


def hash_inbound_token(token: str) -> str:
    """SHA-256 hex of a token. Tokens are random (80 bits), so no salt is needed."""
    if not TOKEN_RE.fullmatch(token):
        raise ValueError("invalid inbound token")
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def inbound_address(workflow: str, org_slug: str, token: str, domain: str) -> str:
    """The inbound address of one organisation and workflow."""
    if (
        not WORKFLOW_RE.fullmatch(workflow)
        or not SLUG_RE.fullmatch(org_slug)
        or not TOKEN_RE.fullmatch(token)
    ):
        raise ValueError("invalid workflow, organisation slug or token")
    return f"{workflow}+{org_slug}.{token}@{domain.lower()}"


def parse_inbound_address(address: str, domain: str) -> InboundRoute | None:
    """``pay+acme.<token>@<domain>`` -> ``InboundRoute``; anything else -> None.

    Slugs never contain a dot, so the token is everything after the last one.
    """
    _, addr = getaddresses([address])[0] if address else ("", "")
    local, at, host = addr.strip().lower().rpartition("@")
    if not at or host != domain.lower():
        return None
    workflow, plus, rest = local.partition("+")
    slug, dot, token = rest.rpartition(".")
    if (
        not plus
        or not dot
        or not WORKFLOW_RE.fullmatch(workflow)
        or not SLUG_RE.fullmatch(slug)
        or not TOKEN_RE.fullmatch(token)
    ):
        return None
    return InboundRoute(workflow, slug, token)


def candidate_routes(
    recipients: Iterable[str], domain: str, workflows: frozenset[str]
) -> list[InboundRoute]:
    """Well-formed addresses of registered workflows, in order, without repeats."""
    routes: list[InboundRoute] = []
    for recipient in recipients:
        route = parse_inbound_address(recipient, domain)
        if route is not None and route.workflow in workflows and route not in routes:
            routes.append(route)
            if len(routes) == MAX_ROUTE_CANDIDATES:
                break
    return routes


async def lookup_inbound_org(
    sessionmaker: async_sessionmaker[AsyncSession], route: InboundRoute
) -> uuid.UUID | None:
    """The organisation this address belongs to, or None (unknown, wrong token, inactive)."""
    async with sessionmaker() as session, session.begin():
        value = await session.scalar(
            text("SELECT inbound_org_id(:workflow, :slug, :token_hash)"),
            {
                "workflow": route.workflow,
                "slug": route.org_slug,
                "token_hash": hash_inbound_token(route.token),
            },
        )
    return value if isinstance(value, uuid.UUID) else None


async def rotate_inbound_token(
    session: AsyncSession, *, workflow: str, actor_type: str, actor_id: str
) -> str:
    """Generate and store a new token for the session's organisation and ``workflow``.

    Returns the token (shown once; only its hash is stored). The previous token, if any,
    stops working when the transaction commits. Writes an audit event without the token.
    """
    org = current_org(session)
    if org is None:
        raise TenantError("rotating an inbound address needs a tenant context")
    if not WORKFLOW_RE.fullmatch(workflow):
        raise ValueError("invalid workflow")
    token = new_inbound_token()
    token_hash = hash_inbound_token(token)
    stmt = insert(InboundAddress).values(org_id=org, workflow=workflow, token_hash=token_hash)
    stmt = stmt.on_conflict_do_update(
        constraint="uq_inbound_addresses_org_id_workflow",
        set_={"token_hash": token_hash, "rotated_at": func.now()},
    )
    await session.execute(stmt)
    await append_audit(
        session,
        action=ROTATED_ACTION,
        actor_type=actor_type,
        actor_id=actor_id,
        entity_type="inbound_address",
        workflow=workflow,
        payload={"workflow": workflow},
    )
    return token
