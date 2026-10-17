# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Anchor audit chain heads outside the database, and read them back for verification.

``anchor_org`` (run nightly by the worker for every organisation with events): reads
the organisation's newest (seq, hash), signs it (``signing.sign_head``) and writes it
to the anchor store under its ``seq``. When an anchor for that ``seq`` already exists
it must hold the same hash; a different hash means the chain was rewritten after it was
anchored, which is reported (``conflict``), never overwritten.

``load_anchors`` lists and verifies every anchor of an organisation. A malformed or
badly signed anchor raises ``AnchorError``: someone tampered with the store, and the
verification fails closed. ``verify_chain(..., anchors=...)`` then checks the database
chain still holds every anchored head (tail truncation detection).
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from pairly_core.audit_anchor.signing import (
    AnchoredHead,
    AnchorError,
    key_id,
    sign_head,
    verify_head,
)
from pairly_core.audit_anchor.store import AnchorStore, anchor_key, org_prefix
from pairly_core.db import TenantError, current_org
from pairly_core.models.audit import AuditEvent

MAX_ANCHORS: Final = 100_000


@dataclass(frozen=True, slots=True)
class AnchorResult:
    org_id: uuid.UUID
    seq: int
    status: str  # written | unchanged | conflict | empty


async def chain_head(session: AsyncSession) -> tuple[int, str] | None:
    """The session organisation's newest (seq, hash), or None for an empty chain."""
    row = (
        await session.execute(
            select(AuditEvent.seq, AuditEvent.hash).order_by(AuditEvent.seq.desc()).limit(1)
        )
    ).first()
    return (int(row.seq), str(row.hash)) if row is not None else None


async def anchor_org(
    session: AsyncSession,
    store: AnchorStore,
    key: Ed25519PrivateKey,
    *,
    now: datetime | None = None,
) -> AnchorResult:
    """Sign and store the head of the session organisation's chain."""
    org = current_org(session)
    if org is None:
        raise TenantError("anchoring needs a tenant context")
    head = await chain_head(session)
    if head is None:
        return AnchorResult(org, 0, "empty")
    seq, head_hash = head
    data = sign_head(key, org_id=org, seq=seq, head_hash=head_hash, at=now or datetime.now(UTC))
    object_key = anchor_key(org, seq)
    if await store.put_new(object_key, data):
        return AnchorResult(org, seq, "written")
    existing = verify_head(
        await store.get(object_key), {key_id(key.public_key()): key.public_key()}
    )
    if existing.hash != head_hash or existing.org_id != org:
        return AnchorResult(org, seq, "conflict")
    return AnchorResult(org, seq, "unchanged")


async def load_anchors(
    store: AnchorStore, org_id: uuid.UUID, keys: Mapping[str, Ed25519PublicKey]
) -> list[AnchoredHead]:
    """Every anchor of ``org_id``, verified, in ``seq`` order. Raises ``AnchorError``."""
    heads: list[AnchoredHead] = []
    for object_key in (await store.list_keys(org_prefix(org_id)))[:MAX_ANCHORS]:
        head = verify_head(await store.get(object_key), keys)
        if head.org_id != org_id or anchor_key(org_id, head.seq) != object_key:
            raise AnchorError("anchor stored under the wrong key")
        heads.append(head)
    return heads


def anchor_map(heads: list[AnchoredHead]) -> dict[int, str]:
    """``seq -> hash`` for ``verify_chain``. Two anchors of one seq must agree."""
    out: dict[int, str] = {}
    for head in heads:
        if out.setdefault(head.seq, head.hash) != head.hash:
            raise AnchorError("two anchors disagree on one seq")
    return out


def public_keys(*keys: Ed25519PublicKey | None) -> dict[str, Ed25519PublicKey]:
    return {key_id(k): k for k in keys if k is not None}
