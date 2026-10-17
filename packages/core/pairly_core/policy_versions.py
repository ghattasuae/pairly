# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Policy as code: immutable, versioned policy documents (brief 2.3.2).

``policy_versions`` stores one row per published version of one policy of one
organisation. A policy is identified by ``kind`` (what validates it, for example
``approval_rules``) and ``name`` (which policy of that kind, for example the approval
type ``pay.invoice``). Versions count up from 1. Rows are never changed: the application
role has SELECT and INSERT only and a trigger rejects UPDATE and DELETE for every role
(migration 0006). Records that used a policy point to the exact row (``id``).

The content is the YAML text as published (so a reviewer sees exactly what was written)
plus its SHA-256. Validation is the caller's job: ``publish_policy_version`` takes a
``validate`` callable that parses and checks the text and raises on any problem, so an
invalid policy is never stored.

Kept generic on purpose: the approvals rules engine uses it now, the policy engine
(WP1.5) and rubrics reuse it.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from pairly_core.audit import ActorType, append_audit
from pairly_core.db import TenantError, current_org
from pairly_core.models.policy import PolicyVersion

POLICY_PUBLISHED = "core.policy.version_published"
MAX_POLICY_BYTES = 64 * 1024

_KIND_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_NAME_RE = re.compile(r"^[a-z][a-z0-9_.]{0,127}$")


class PolicyError(ValueError):
    """A policy document is invalid. The message is safe to show to an admin."""


@dataclass(frozen=True, slots=True)
class PublishedPolicy:
    id: uuid.UUID
    kind: str
    name: str
    version: int
    content: str
    content_hash: str
    created_at: datetime

    @classmethod
    def from_row(cls, row: PolicyVersion) -> PublishedPolicy:
        return cls(
            id=row.id,
            kind=row.kind,
            name=row.name,
            version=row.version,
            content=row.content,
            content_hash=row.content_hash,
            created_at=row.created_at,
        )


def content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _check_ids(kind: str, name: str) -> None:
    if not _KIND_RE.fullmatch(kind):
        raise PolicyError("invalid policy kind")
    if not _NAME_RE.fullmatch(name):
        raise PolicyError("invalid policy name")


async def latest_policy(session: AsyncSession, kind: str, name: str) -> PublishedPolicy | None:
    _check_ids(kind, name)
    row = await session.scalar(
        select(PolicyVersion)
        .where(PolicyVersion.kind == kind, PolicyVersion.name == name)
        .order_by(PolicyVersion.version.desc())
        .limit(1)
    )
    return PublishedPolicy.from_row(row) if row is not None else None


async def get_policy(session: AsyncSession, policy_id: uuid.UUID) -> PublishedPolicy | None:
    row = await session.scalar(select(PolicyVersion).where(PolicyVersion.id == policy_id))
    return PublishedPolicy.from_row(row) if row is not None else None


async def list_policy_versions(
    session: AsyncSession, kind: str, name: str, *, limit: int = 100
) -> list[PublishedPolicy]:
    _check_ids(kind, name)
    rows = await session.scalars(
        select(PolicyVersion)
        .where(PolicyVersion.kind == kind, PolicyVersion.name == name)
        .order_by(PolicyVersion.version.desc())
        .limit(limit)
    )
    return [PublishedPolicy.from_row(r) for r in rows]


async def latest_policies(
    session: AsyncSession, kind: str, *, limit: int = 200
) -> list[PublishedPolicy]:
    """The latest version of every policy of ``kind`` in the organisation, by name."""
    if not _KIND_RE.fullmatch(kind):
        raise PolicyError("invalid policy kind")
    rows = await session.scalars(
        select(PolicyVersion)
        .where(PolicyVersion.kind == kind)
        .ext(distinct_on(PolicyVersion.name))
        .order_by(PolicyVersion.name, PolicyVersion.version.desc())
        .limit(max(1, min(limit, 500)))
    )
    return [PublishedPolicy.from_row(r) for r in rows]


async def publish_policy_version(
    session: AsyncSession,
    *,
    kind: str,
    name: str,
    content: str,
    validate: Callable[[str], Any],
    actor_type: ActorType,
    actor_id: str,
) -> PublishedPolicy:
    """Validate ``content`` and store it as the next version, with an audit event.

    Publishing text identical to the latest version returns that version (no new row).
    Two concurrent publishers serialise on a per-policy advisory lock; the unique
    constraint is the last line of defence.
    """
    _check_ids(kind, name)
    org = current_org(session)
    if org is None:
        raise TenantError("policies need a tenant context")
    if not isinstance(content, str) or not content.strip():
        raise PolicyError("the policy is empty")
    if len(content.encode("utf-8")) > MAX_POLICY_BYTES:
        raise PolicyError("the policy is too large")
    validate(content)
    digest = content_hash(content)

    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"pairly.policy:{org}:{kind}:{name}"},
    )
    latest = await latest_policy(session, kind, name)
    if latest is not None and latest.content_hash == digest:
        return latest
    version = 1 if latest is None else latest.version + 1
    new_id = uuid.uuid4()
    await session.execute(
        pg_insert(PolicyVersion).values(
            id=new_id,
            org_id=org,
            kind=kind,
            name=name,
            version=version,
            content=content,
            content_hash=digest,
            created_by_type=actor_type.value,
            created_by_id=actor_id,
        )
    )
    await append_audit(
        session,
        action=POLICY_PUBLISHED,
        actor_type=actor_type,
        actor_id=actor_id,
        entity_type="policy_version",
        entity_id=new_id,
        payload={"kind": kind, "name": name, "version": version, "content_hash": digest},
    )
    stored = await get_policy(session, new_id)
    if stored is None:  # pragma: no cover - RLS would have refused the insert
        raise TenantError("policy version not visible after insert")
    return stored
