# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Verification of Better Auth JWTs against its JWKS endpoint.

Rejected: tokens with ``alg`` none or any symmetric (HS*) algorithm, algorithms outside
the configured allowlist, a key id that is missing or not in the JWKS, an algorithm that
does not match the key, a bad signature, expired or not-yet-valid tokens, and a wrong
issuer or audience. ``exp``, ``iat``, ``sub``, ``iss`` and ``aud`` are required.

Keys are cached for ``auth_jwks_cache_seconds``. An unknown key id triggers one refetch
(rate limited by ``auth_jwks_min_refresh_seconds``) so key rotation works without a
restart, while a flood of random key ids cannot hammer the auth server. If the JWKS
cannot be fetched once the cache is stale, verification fails closed.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import httpx
import jwt
import structlog
from jwt import PyJWK

from pairly_core.config import Settings
from pairly_core.errors import ServiceUnavailableError, UnauthenticatedError

log = structlog.get_logger(__name__)

MAX_TOKEN_BYTES = 8192
MAX_JWKS_BYTES = 64 * 1024
_SYMMETRIC_KTY = frozenset({"oct"})


class TokenError(UnauthenticatedError):
    code = "invalid_token"
    title = "Invalid or expired token"


class JWKSUnavailableError(ServiceUnavailableError):
    code = "jwks_unavailable"
    title = "Authentication keys unavailable"


@dataclass(frozen=True, slots=True)
class VerifiedToken:
    subject: str
    key_id: str
    claims: Mapping[str, Any]


class JWKSCache:
    """Async, rotation-aware cache of the signing keys published at ``url``."""

    def __init__(
        self,
        url: str,
        *,
        allowed_algorithms: Sequence[str],
        ttl_seconds: float = 300,
        min_refresh_seconds: float = 30,
        client: httpx.AsyncClient | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._url = url
        self._allowed = frozenset(allowed_algorithms)
        self._ttl = ttl_seconds
        self._min_refresh = min_refresh_seconds
        self._client = client or httpx.AsyncClient(timeout=5.0, follow_redirects=False)
        self._owns_client = client is None
        self._clock = clock
        self._keys: dict[str, PyJWK] = {}
        self._fetched_at: float | None = None
        self._last_attempt: float | None = None
        self._lock = asyncio.Lock()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def _stale(self) -> bool:
        return self._fetched_at is None or self._clock() - self._fetched_at >= self._ttl

    async def get(self, kid: str) -> PyJWK:
        if self._stale():
            await self._refresh(force=False)
        key = self._keys.get(kid)
        if key is None:
            await self._refresh(force=True)
            key = self._keys.get(kid)
        if key is None:
            raise TokenError("unknown signing key")
        return key

    async def _refresh(self, *, force: bool) -> None:
        async with self._lock:
            now = self._clock()
            if not force and not self._stale():
                return  # another task refreshed while we waited
            recent = self._last_attempt is not None and now - self._last_attempt < self._min_refresh
            if force and recent:
                return
            self._last_attempt = now
            try:
                keys = await self._fetch()
            except Exception as exc:
                log.warning("jwks_fetch_failed", error=type(exc).__name__)
                if self._stale():
                    # Fail closed: never keep trusting keys we can no longer confirm.
                    self._keys = {}
                    raise JWKSUnavailableError() from None
                return
            self._keys = keys
            self._fetched_at = now

    async def _fetch(self) -> dict[str, PyJWK]:
        response = await self._client.get(self._url, headers={"Accept": "application/json"})
        response.raise_for_status()
        if len(response.content) > MAX_JWKS_BYTES:
            raise ValueError("JWKS document too large")
        document = response.json()
        raw_keys = document.get("keys") if isinstance(document, dict) else None
        if not isinstance(raw_keys, list):
            raise ValueError("JWKS document has no keys array")
        keys: dict[str, PyJWK] = {}
        for raw in raw_keys:
            key = self._parse_key(raw)
            if key is not None and key.key_id is not None:
                keys[key.key_id] = key
        return keys

    def _parse_key(self, raw: object) -> PyJWK | None:
        if not isinstance(raw, dict):
            return None
        kid = raw.get("kid")
        if not isinstance(kid, str) or not kid:
            return None
        if raw.get("kty") in _SYMMETRIC_KTY or raw.get("use", "sig") != "sig":
            return None
        try:
            key = PyJWK(raw)
        except jwt.PyJWTError:
            log.warning("jwks_key_skipped", kid=kid)
            return None
        if key.algorithm_name not in self._allowed:
            return None
        return key


class TokenVerifier:
    """Verifies bearer tokens issued by Better Auth."""

    def __init__(
        self,
        *,
        jwks: JWKSCache,
        issuer: str,
        audience: str,
        algorithms: Sequence[str],
        leeway_seconds: float = 10,
    ) -> None:
        self._jwks = jwks
        self._issuer = issuer
        self._audience = audience
        self._algorithms = frozenset(algorithms)
        self._leeway = leeway_seconds
        if not self._algorithms or any(a.upper().startswith("HS") for a in self._algorithms):
            raise ValueError("only asymmetric algorithms may be configured")

    @classmethod
    def from_settings(
        cls, settings: Settings, client: httpx.AsyncClient | None = None
    ) -> TokenVerifier:
        jwks = JWKSCache(
            settings.auth_jwks_url,
            allowed_algorithms=settings.auth_algorithms,
            ttl_seconds=settings.auth_jwks_cache_seconds,
            min_refresh_seconds=settings.auth_jwks_min_refresh_seconds,
            client=client,
        )
        return cls(
            jwks=jwks,
            issuer=settings.auth_issuer,
            audience=settings.auth_audience,
            algorithms=settings.auth_algorithms,
            leeway_seconds=settings.auth_leeway_seconds,
        )

    async def aclose(self) -> None:
        await self._jwks.aclose()

    async def verify(self, token: str) -> VerifiedToken:
        if not token or len(token.encode()) > MAX_TOKEN_BYTES:
            raise TokenError()
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError:
            raise TokenError("malformed token") from None
        alg = header.get("alg")
        if not isinstance(alg, str) or alg not in self._algorithms:
            raise TokenError("unsupported token algorithm")
        kid = header.get("kid")
        if not isinstance(kid, str) or not kid:
            raise TokenError("token has no key id")
        key = await self._jwks.get(kid)
        if key.algorithm_name != alg:
            raise TokenError("token algorithm does not match its key")
        try:
            claims: dict[str, Any] = jwt.decode(
                token,
                key=key.key,
                algorithms=[alg],
                audience=self._audience,
                issuer=self._issuer,
                leeway=self._leeway,
                options={"require": ["exp", "iat", "sub", "iss", "aud"]},
            )
        except jwt.ExpiredSignatureError:
            raise TokenError("token expired") from None
        except jwt.PyJWTError:
            raise TokenError() from None
        subject = claims.get("sub")
        if not isinstance(subject, str) or not subject or len(subject) > 255:
            raise TokenError()
        return VerifiedToken(subject=subject, key_id=kid, claims=claims)
