# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Create, list, revoke and authenticate API keys.

Rules:

* Only a person holding ``core.api_keys.manage`` (step-up) creates or revokes keys.
* Scopes are permission names the registry knows, whose action is ``read``,
  ``propose`` or ``upload`` (``rbac.api_key_may_hold``): never approve, decide, manage,
  export or another final action. The creator must hold every scope (no escalation).
* Every key expires (default 90 days, at most ``api_key_max_days``).
* ``pk_test_`` keys can only be created in, and only authenticate against, sandbox
  organisations (``organisations.sandbox``).

Authentication (``authenticate_api_key``), for ``Authorization: Bearer pk_...``:

1. Parse the token. Malformed: 401.
2. Find the key's organisation with ``api_key_org_id(prefix)`` (SECURITY DEFINER; the
   request is not bound to an organisation yet). Unknown: 401.
3. Bind the session to that organisation (RLS) and load the key and the organisation.
4. Check the secret's HMAC (constant time). Only then: an ``X-Pairly-Org`` header, if
   sent, must name the key's organisation (else 403); then revoked, expired, organisation
   suspended, test key outside a sandbox. Each is refused (401 or 403).
5. Rate limit per key (429 with ``Retry-After``).
6. Record ``last_used_at`` at most once a minute, in its own short transaction.

The result is an ``api_key`` actor whose permissions are the key's scopes that the
registry still knows and that an API key may hold.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final

from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from pairly_core.api_keys.models import ApiKey
from pairly_core.api_keys.ratelimit import RateLimiter
from pairly_core.api_keys.tokens import (
    KeyMode,
    get_pepper,
    hash_secret,
    new_key,
    parse_token,
    verify_secret,
)
from pairly_core.audit import audit_actor_action
from pairly_core.db import current_org, set_tenant
from pairly_core.errors import (
    BadRequestError,
    ForbiddenError,
    NotFoundError,
    UnauthenticatedError,
)
from pairly_core.models import Organisation
from pairly_core.rbac import (
    Actor,
    ActorKind,
    PermissionDeniedError,
    RoleRegistry,
    api_key_may_hold,
)

DEFAULT_DAYS: Final = 90
LAST_USED_RESOLUTION: Final = timedelta(seconds=60)
MAX_SCOPES: Final = 100


class ApiKeyAction:
    CREATED: Final = "core.api_key.created"
    REVOKED: Final = "core.api_key.revoked"


class InvalidApiKeyError(UnauthenticatedError):
    code = "invalid_api_key"
    title = "Invalid API key"


class ApiKeyRevokedError(UnauthenticatedError):
    code = "api_key_revoked"
    title = "This API key has been revoked"


class ApiKeyExpiredError(UnauthenticatedError):
    code = "api_key_expired"
    title = "This API key has expired"


class TestKeyNotAllowedError(ForbiddenError):
    code = "test_key_not_allowed"
    title = "Test API keys only work in sandbox organisations"


@dataclass(frozen=True, slots=True)
class CreatedKey:
    key: ApiKey
    token: str  # the full key, returned once; never stored or logged


def _require_person(actor: Actor) -> None:
    if actor.kind is not ActorKind.USER:
        raise PermissionDeniedError("only a person can manage API keys")


def check_scopes(registry: RoleRegistry, creator: Actor, scopes: Iterable[str]) -> list[str]:
    """Validated, sorted, de-duplicated scopes, or ``BadRequestError``/403."""
    clean = sorted(set(scopes))
    if not clean:
        raise BadRequestError("an API key needs at least one scope")
    if len(clean) > MAX_SCOPES:
        raise BadRequestError("too many scopes")
    for scope in clean:
        if scope not in registry:
            raise BadRequestError(f"unknown scope {scope!r}")
        if not api_key_may_hold(scope):
            raise BadRequestError(
                f"an API key cannot hold {scope!r}: keys may only read, propose and upload"
            )
        if not creator.has(scope):
            raise PermissionDeniedError(f"you cannot give a key a scope you do not hold: {scope}")
    return clean


async def create_api_key(
    session: AsyncSession,
    registry: RoleRegistry,
    actor: Actor,
    *,
    name: str,
    scopes: Sequence[str],
    mode: KeyMode = KeyMode.LIVE,
    expires_in_days: int = DEFAULT_DAYS,
    max_days: int = 365,
    rate_limit_per_minute: int | None = None,
) -> CreatedKey:
    _require_person(actor)
    clean_name = name.strip()
    if not 1 <= len(clean_name) <= 100:
        raise BadRequestError("the name must be 1 to 100 characters")
    if not 1 <= expires_in_days <= max_days:
        raise BadRequestError(f"a key must expire within 1 to {max_days} days")
    clean_scopes = check_scopes(registry, actor, scopes)
    org = await session.scalar(select(Organisation).where(Organisation.id == actor.org_id))
    if org is None:  # pragma: no cover - the actor was loaded in this org
        raise NotFoundError("organisation not found")
    if mode is KeyMode.TEST and not org.sandbox:
        raise TestKeyNotAllowedError("test keys can only be created in a sandbox organisation")
    now = datetime.now(UTC)
    pepper = get_pepper()
    for _attempt in range(3):
        fresh = new_key(mode)
        row = ApiKey(
            id=uuid.uuid4(),
            org_id=actor.org_id,
            name=clean_name,
            prefix=fresh.prefix,
            mode=mode.value,
            secret_hash=hash_secret(pepper, fresh.prefix, fresh.secret),
            scopes=clean_scopes,
            created_by=actor.id,
            created_at=now,
            expires_at=now + timedelta(days=expires_in_days),
            rate_limit_per_minute=rate_limit_per_minute,
        )
        try:
            async with session.begin_nested():
                session.add(row)
                await session.flush()
        except IntegrityError:
            continue  # prefix collision (one in about 2.8e12): draw another
        break
    else:  # pragma: no cover - three collisions in a row
        raise BadRequestError("could not create a unique key; try again")
    await audit_actor_action(
        session,
        actor,
        ApiKeyAction.CREATED,
        entity_type="api_key",
        entity_id=row.id,
        payload={
            "prefix": row.prefix,
            "name": row.name,
            "mode": row.mode,
            "scopes": clean_scopes,
            "expires_at": row.expires_at.isoformat(),
        },
    )
    return CreatedKey(key=row, token=fresh.token)


async def list_api_keys(session: AsyncSession) -> list[ApiKey]:
    return list(await session.scalars(select(ApiKey).order_by(ApiKey.created_at.desc())))


async def revoke_api_key(session: AsyncSession, actor: Actor, key_id: uuid.UUID) -> ApiKey:
    _require_person(actor)
    row = await session.scalar(select(ApiKey).where(ApiKey.id == key_id).with_for_update())
    if row is None:
        raise NotFoundError("API key not found")
    if row.revoked_at is not None:
        return row
    row.revoked_at = datetime.now(UTC)
    row.revoked_by = actor.id
    await session.flush()
    await audit_actor_action(
        session,
        actor,
        ApiKeyAction.REVOKED,
        entity_type="api_key",
        entity_id=row.id,
        payload={"prefix": row.prefix, "name": row.name},
    )
    return row


def key_permissions(registry: RoleRegistry, scopes: Iterable[str]) -> frozenset[str]:
    """The permissions a key's scopes grant today (unknown or forbidden ones drop out)."""
    return frozenset(s for s in scopes if s in registry and api_key_may_hold(s))


async def authenticate_api_key(
    session: AsyncSession,
    registry: RoleRegistry,
    token: str,
    *,
    org_header: uuid.UUID | None,
    limiter: RateLimiter,
    default_rate_limit: int,
) -> Actor:
    """The ``api_key`` actor for ``token``, with ``session`` bound to its organisation."""
    parsed = parse_token(token)
    if parsed is None:
        raise InvalidApiKeyError()
    if current_org(session) is not None or session.in_transaction():
        raise RuntimeError("the API key lookup must start without an organisation")
    org_id = await session.scalar(select(func.api_key_org_id(parsed.prefix)))
    await session.rollback()
    if not isinstance(org_id, uuid.UUID):
        raise InvalidApiKeyError()
    set_tenant(session, org_id, None)
    row = await session.scalar(select(ApiKey).where(ApiKey.prefix == parsed.prefix))
    org = await session.scalar(select(Organisation).where(Organisation.id == org_id))
    if row is None or org is None:
        raise InvalidApiKeyError()
    if not verify_secret(get_pepper(), row.prefix, parsed.secret, row.secret_hash):
        raise InvalidApiKeyError()
    # Only after the secret checks out, so a bare prefix reveals nothing about its org.
    if org_header is not None and org_header != org_id:
        raise ForbiddenError("no access to this organisation")
    now = await session.scalar(select(func.now()))
    if row.revoked_at is not None:
        raise ApiKeyRevokedError()
    if now is None or row.expires_at <= now:
        raise ApiKeyExpiredError()
    if org.status != "active":
        raise ForbiddenError("no access to this organisation")
    if row.mode == KeyMode.TEST.value and not org.sandbox:
        raise TestKeyNotAllowedError()
    limiter.check(str(row.id), row.rate_limit_per_minute or default_rate_limit)
    if row.last_used_at is None or now - row.last_used_at >= LAST_USED_RESOLUTION:
        await session.execute(
            update(ApiKey)
            .where(
                ApiKey.id == row.id,
                or_(
                    ApiKey.last_used_at.is_(None), ApiKey.last_used_at < now - LAST_USED_RESOLUTION
                ),
            )
            .values(last_used_at=func.now())
            .execution_options(synchronize_session=False)
        )
    # A short transaction of its own: the request's work starts a new one (same tenant),
    # so a long request never holds the key row's lock.
    await session.commit()
    return Actor(
        kind=ActorKind.API_KEY,
        id=row.id,
        org_id=org_id,
        grants=(),
        permissions=key_permissions(registry, row.scopes),
    )
