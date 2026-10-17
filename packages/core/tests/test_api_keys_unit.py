# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""API keys without a database: token format, pepper, hashing, scopes, rate limit."""

from __future__ import annotations

import base64
import secrets
import uuid

import pytest

from pairly_core.api_keys.ratelimit import RateLimitedError, RateLimiter
from pairly_core.api_keys.service import check_scopes, key_permissions
from pairly_core.api_keys.tokens import (
    PEPPER_ENV,
    ApiKeyConfigError,
    KeyMode,
    hash_secret,
    load_pepper,
    looks_like_api_key,
    new_key,
    parse_token,
    verify_secret,
)
from pairly_core.errors import BadRequestError
from pairly_core.rbac import (
    Actor,
    ActorKind,
    PermissionDeniedError,
    api_key_may_hold,
    authorize,
    build_registry,
)

PEPPER = secrets.token_bytes(32)


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode()


# ------------------------------------------------------------------ tokens


@pytest.mark.parametrize("mode", list(KeyMode))
def test_new_key_round_trips_through_parse(mode: KeyMode) -> None:
    fresh = new_key(mode)
    assert fresh.token.startswith(f"pk_{mode.value}_")
    assert looks_like_api_key(fresh.token)
    parsed = parse_token(fresh.token)
    assert parsed is not None
    assert parsed.prefix == fresh.prefix
    assert parsed.mode is mode
    assert len(parsed.secret) == 43
    assert parsed.secret == fresh.secret
    assert new_key(mode).token != fresh.token
    assert fresh.secret not in repr(fresh)


def test_secrets_with_underscores_parse_back() -> None:
    # Regression: the secret alphabet includes "_", so splitting the token on "_"
    # recovered the wrong secret for about half of all keys.
    for _ in range(200):
        fresh = new_key(KeyMode.LIVE)
        parsed = parse_token(fresh.token)
        assert parsed is not None
        assert (parsed.prefix, parsed.secret) == (fresh.prefix, fresh.secret)


@pytest.mark.parametrize(
    "token",
    [
        "",
        "pk_live_abc",
        "pk_prod_abcdefgh_" + "a" * 43,
        "pk_live_ABCDEFGH_" + "a" * 43,
        "pk_live_abcdefgh_" + "a" * 42,
        "pk_live_abcdefgh_" + "a" * 43 + "x",
        "pk_live_abcdefgh_" + "a" * 42 + "!",
        "eyJhbGciOiJFZERTQSJ9.e30.sig",
    ],
)
def test_malformed_tokens_do_not_parse(token: str) -> None:
    assert parse_token(token) is None


def test_hash_binds_prefix_and_secret() -> None:
    fresh = new_key(KeyMode.LIVE)
    parsed = parse_token(fresh.token)
    assert parsed is not None
    stored = hash_secret(PEPPER, parsed.prefix, parsed.secret)
    assert len(stored) == 64
    assert verify_secret(PEPPER, parsed.prefix, parsed.secret, stored)
    # Another prefix, another secret or another pepper never matches.
    other = new_key(KeyMode.LIVE)
    assert not verify_secret(PEPPER, other.prefix, parsed.secret, stored)
    flipped = parsed.secret[:-1] + ("A" if parsed.secret[-1] != "A" else "B")
    assert not verify_secret(PEPPER, parsed.prefix, flipped, stored)
    assert not verify_secret(secrets.token_bytes(32), parsed.prefix, parsed.secret, stored)


def test_pepper_fails_closed_without_echoing_the_value() -> None:
    with pytest.raises(ApiKeyConfigError, match=PEPPER_ENV):
        load_pepper({})
    with pytest.raises(ApiKeyConfigError, match="base64"):
        load_pepper({PEPPER_ENV: "not base64 at all!"})
    short = _b64(secrets.token_bytes(16))
    with pytest.raises(ApiKeyConfigError, match="32 bytes") as exc:
        load_pepper({PEPPER_ENV: short})
    assert short not in str(exc.value)
    with pytest.raises(ApiKeyConfigError, match="placeholder"):
        load_pepper({PEPPER_ENV: _b64(b"\x00" * 32)})
    assert load_pepper({PEPPER_ENV: _b64(PEPPER)}) == PEPPER


# ------------------------------------------------------------------ scopes


def _user(*permissions: str) -> Actor:
    return Actor(
        kind=ActorKind.USER,
        id=uuid.uuid4(),
        org_id=uuid.uuid4(),
        grants=(),
        permissions=frozenset(permissions),
    )


def test_keys_may_only_read_propose_and_upload() -> None:
    assert api_key_may_hold("core.document.read")
    assert api_key_may_hold("core.document.upload")
    for permission in ("core.approval.decide", "core.api_keys.manage", "core.audit.export"):
        assert not api_key_may_hold(permission)


def test_check_scopes_rules() -> None:
    registry = build_registry([])
    creator = _user("core.document.read", "core.document.upload", "core.api_keys.manage")
    assert check_scopes(
        registry, creator, ["core.document.upload", "core.document.read", "core.document.read"]
    ) == ["core.document.read", "core.document.upload"]
    with pytest.raises(BadRequestError, match="at least one"):
        check_scopes(registry, creator, [])
    with pytest.raises(BadRequestError, match="unknown scope"):
        check_scopes(registry, creator, ["core.nothing.read"])
    with pytest.raises(BadRequestError, match="only read, propose and upload"):
        check_scopes(registry, creator, ["core.api_keys.manage"])
    # No escalation: a creator cannot hand out a scope it does not hold.
    with pytest.raises(PermissionDeniedError):
        check_scopes(registry, _user("core.document.read"), ["core.document.upload"])


def test_key_actor_never_gets_a_final_action() -> None:
    registry = build_registry([])
    # A key row that somehow names manage (or an unknown scope) still grants neither.
    perms = key_permissions(registry, ["core.document.read", "core.api_keys.manage", "x.y.read"])
    assert perms == frozenset({"core.document.read"})
    actor = Actor(
        kind=ActorKind.API_KEY,
        id=uuid.uuid4(),
        org_id=uuid.uuid4(),
        grants=(),
        permissions=frozenset({"core.document.read", "core.api_keys.manage"}),
    )
    authorize(actor, "core.document.read")
    with pytest.raises(PermissionDeniedError):
        authorize(actor, "core.api_keys.manage")


# ------------------------------------------------------------------ rate limit


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_rate_limiter_bursts_then_refills() -> None:
    clock = _Clock()
    limiter = RateLimiter(clock=clock)
    for _ in range(3):
        limiter.check("k", 3)
    with pytest.raises(RateLimitedError) as exc:
        limiter.check("k", 3)
    assert exc.value.status == 429
    assert exc.value.headers == {"Retry-After": "20"}
    # Another key has its own bucket.
    limiter.check("other", 3)
    clock.now += 20
    limiter.check("k", 3)
    with pytest.raises(RateLimitedError):
        limiter.check("k", 3)


def test_rate_limiter_is_bounded_and_refuses_zero() -> None:
    limiter = RateLimiter(max_keys=2, clock=_Clock())
    limiter.check("a", 1)
    limiter.check("b", 1)
    limiter.check("c", 1)  # drops "a", the least recently used
    limiter.check("a", 1)  # a fresh bucket again
    with pytest.raises(RateLimitedError):
        limiter.check("z", 0)
