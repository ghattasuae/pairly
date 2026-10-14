# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Small helpers the Pay services share: names, references, who did it, audit events and
decision records."""

from __future__ import annotations

import re
import secrets
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from pairly_core.audit import ActorType, append_audit, audit_actor_action
from pairly_core.crypto.types import Plaintext, unseal
from pairly_core.decisions.models import DecisionKind, DecisionRecord
from pairly_core.decisions.service import DecisionDraft, record_decision
from pairly_core.rbac import Actor, ActorKind

WORKFLOW: Final = "pay"
VENDOR_ENTITY: Final = "pay.vendor"
TICKET_ENTITY: Final = "pay.bank_change"
INVITE_ENTITY: Final = "pay.vendor_invite"
SYSTEM_ACTOR: Final = "pairly.pay"
# The external person filling in the onboarding form (``ActorType.CANDIDATE`` is the
# engine's type for an external person acting through a workflow portal).
VENDOR_ACTOR_PREFIX: Final = "pay_vendor_invite"

_REF_ALPHABET: Final = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
_CONTROL: Final = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def opened(row: Any, column: str) -> Any:
    """The plaintext of an encrypted column, also right after it was set in this
    session (the attribute then still holds the ``Plaintext`` to encrypt)."""
    value = getattr(row, column)
    if isinstance(value, Plaintext):
        return value.value
    return unseal(row, column)


def new_reference(prefix: str) -> str:
    """A short human reference (``VEN-7K2QX9``); unique per org by a constraint."""
    return prefix + "-" + "".join(secrets.choice(_REF_ALPHABET) for _ in range(6))


def clean_text(value: str | None, limit: int) -> str:
    text = _CONTROL.sub("", value or "").strip()
    return text[:limit]


@dataclass(frozen=True, slots=True)
class By:
    """Who did something: an actor type and id, and the person when it was one."""

    actor_type: ActorType
    actor_id: str
    user_id: uuid.UUID | None = None

    @classmethod
    def of(cls, actor: Actor) -> By:
        if actor.kind is ActorKind.USER:
            return cls(ActorType.USER, str(actor.id), actor.id)
        kind = {
            ActorKind.AGENT: ActorType.AGENT,
            ActorKind.API_KEY: ActorType.API_KEY,
            ActorKind.CANDIDATE: ActorType.CANDIDATE,
        }[actor.kind]
        return cls(kind, str(actor.id))

    @classmethod
    def system(cls, name: str = SYSTEM_ACTOR) -> By:
        return cls(ActorType.SYSTEM, name)

    @classmethod
    def vendor(cls, invite_id: uuid.UUID) -> By:
        return cls(ActorType.CANDIDATE, f"{VENDOR_ACTOR_PREFIX}:{invite_id}")


async def audit(
    session: AsyncSession,
    by: By | Actor,
    action: str,
    *,
    entity_type: str,
    entity_id: uuid.UUID,
    payload: Mapping[str, Any] | None = None,
) -> None:
    if isinstance(by, Actor):
        await audit_actor_action(
            session,
            by,
            action,
            entity_type=entity_type,
            entity_id=entity_id,
            workflow=WORKFLOW,
            payload=payload,
        )
        return
    await append_audit(
        session,
        action=action,
        actor_type=by.actor_type,
        actor_id=by.actor_id,
        entity_type=entity_type,
        entity_id=entity_id,
        workflow=WORKFLOW,
        payload=payload,
    )


async def record_step(
    session: AsyncSession,
    by: By,
    *,
    entity_type: str,
    entity_id: uuid.UUID,
    action: str,
    inputs: Mapping[str, Any],
    output: Mapping[str, Any],
    reason: str | None = None,
    sensitive: Mapping[str, Any] | None = None,
    approval_ids: Sequence[uuid.UUID] = (),
) -> str:
    """A decision record of a person's (or the system's) step, chained to the entity's
    newest record. Returns its id."""
    stored = await record_decision(
        session,
        DecisionDraft(
            kind=DecisionKind.FINAL_ACTION,
            workflow=WORKFLOW,
            entity_type=entity_type,
            entity_id=str(entity_id),
            actor_type=by.actor_type,
            actor_id=by.actor_id,
            inputs=inputs,
            output=output,
            final_action=action,
            reason=reason,
            sensitive=sensitive,
            approval_ids=approval_ids,
        ),
    )
    return stored.id


async def decision_ids_of(
    session: AsyncSession, entity_type: str, entity_id: uuid.UUID
) -> list[str]:
    rows = await session.scalars(
        select(DecisionRecord.id)
        .where(
            DecisionRecord.workflow == WORKFLOW,
            DecisionRecord.entity_type == entity_type,
            DecisionRecord.entity_id == str(entity_id),
        )
        .order_by(DecisionRecord.decided_at, DecisionRecord.id)
    )
    return list(rows)
