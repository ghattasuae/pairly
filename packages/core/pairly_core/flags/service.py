# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Read and change an organisation's feature flags (in the caller's tenant session).

* ``is_enabled(session, key)``: the override if the organisation has one, else the
  declared default. Unknown keys raise ``FlagError`` (fail loudly, never a guess).
* ``set_flag`` and ``reset_flag``: a person with ``core.flags.manage`` changes a flag;
  every change is audited (``core.flag.changed``) with the old and new value.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from pairly_core.audit import audit_actor_action
from pairly_core.db import TenantError, current_org
from pairly_core.flags.models import FeatureFlag
from pairly_core.flags.registry import FlagRegistry, flag_registry
from pairly_core.rbac import Actor, ActorKind, PermissionDeniedError

FLAG_CHANGED: Final = "core.flag.changed"


@dataclass(frozen=True, slots=True)
class EffectiveFlag:
    key: str
    description: str
    default: bool
    enabled: bool
    overridden: bool


def _require_tenant(session: AsyncSession) -> None:
    if current_org(session) is None:
        raise TenantError("feature flags need a tenant context")


async def _override(session: AsyncSession, key: str) -> bool | None:
    value = await session.scalar(select(FeatureFlag.enabled).where(FeatureFlag.key == key))
    return None if value is None else bool(value)


async def is_enabled(
    session: AsyncSession, key: str, *, registry: FlagRegistry | None = None
) -> bool:
    """Whether ``key`` is on for the session's organisation."""
    _require_tenant(session)
    spec = (registry or flag_registry()).require(key)
    override = await _override(session, key)
    return spec.default if override is None else override


async def effective_flags(
    session: AsyncSession, *, registry: FlagRegistry | None = None
) -> list[EffectiveFlag]:
    """Every declared flag with its value for the session's organisation."""
    _require_tenant(session)
    overrides = {
        key: bool(enabled)
        for key, enabled in await session.execute(select(FeatureFlag.key, FeatureFlag.enabled))
    }
    return [
        EffectiveFlag(
            key=spec.key,
            description=spec.description,
            default=spec.default,
            enabled=overrides.get(spec.key, spec.default),
            overridden=spec.key in overrides,
        )
        for spec in (registry or flag_registry()).flags
    ]


def _require_person(actor: Actor) -> None:
    if actor.kind is not ActorKind.USER:
        raise PermissionDeniedError("only a person can change feature flags")


async def set_flag(
    session: AsyncSession,
    actor: Actor,
    key: str,
    enabled: bool,
    *,
    registry: FlagRegistry | None = None,
) -> EffectiveFlag:
    """Override ``key`` for the actor's organisation. Audited when the value changes."""
    _require_person(actor)
    spec = (registry or flag_registry()).require(key)
    before = await is_enabled(session, key, registry=registry)
    await session.execute(
        insert(FeatureFlag)
        .values(
            org_id=actor.org_id,
            key=key,
            enabled=enabled,
            updated_by=actor.id,
            updated_at=datetime.now(UTC),
        )
        .on_conflict_do_update(
            constraint="uq_feature_flags_org_key",
            set_={"enabled": enabled, "updated_by": actor.id, "updated_at": datetime.now(UTC)},
        )
    )
    if before != enabled:
        await audit_actor_action(
            session,
            actor,
            FLAG_CHANGED,
            entity_type="feature_flag",
            entity_id=key,
            payload={"key": key, "from": before, "to": enabled, "default": spec.default},
        )
    return EffectiveFlag(spec.key, spec.description, spec.default, enabled, True)


async def reset_flag(
    session: AsyncSession, actor: Actor, key: str, *, registry: FlagRegistry | None = None
) -> EffectiveFlag:
    """Remove the organisation's override: ``key`` follows its declared default again."""
    _require_person(actor)
    spec = (registry or flag_registry()).require(key)
    before = await is_enabled(session, key, registry=registry)
    await session.execute(delete(FeatureFlag).where(FeatureFlag.key == key))
    if before != spec.default:
        await audit_actor_action(
            session,
            actor,
            FLAG_CHANGED,
            entity_type="feature_flag",
            entity_id=key,
            payload={"key": key, "from": before, "to": spec.default, "default": spec.default},
        )
    return EffectiveFlag(spec.key, spec.description, spec.default, spec.default, False)
