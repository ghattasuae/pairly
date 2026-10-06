# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""JWT verification: the happy path and every rejection rule."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey, generate_private_key
from jwt.algorithms import OKPAlgorithm, RSAAlgorithm

from pairly_core.auth import JWKSCache, JWKSUnavailableError, TokenError, TokenVerifier

ISSUER = "https://auth.test.pairly"
AUDIENCE = "https://api.test.pairly"
JWKS_URL = "https://auth.test.pairly/api/auth/jwks"
ALGS = ["EdDSA", "RS256"]


def _ed_jwk(key: Ed25519PrivateKey, kid: str) -> dict[str, Any]:
    jwk: dict[str, Any] = json.loads(OKPAlgorithm.to_jwk(key.public_key()))
    return jwk | {"kid": kid, "alg": "EdDSA", "use": "sig"}


def _rsa_jwk(key: RSAPrivateKey, kid: str) -> dict[str, Any]:
    jwk: dict[str, Any] = json.loads(RSAAlgorithm.to_jwk(key.public_key()))
    return jwk | {"kid": kid, "alg": "RS256", "use": "sig"}


class JWKSServer:
    """In-memory JWKS endpoint served through an httpx mock transport."""

    def __init__(self) -> None:
        self.keys: list[dict[str, Any]] = []
        self.hits = 0
        self.fail = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert str(request.url) == JWKS_URL
        self.hits += 1
        if self.fail:
            return httpx.Response(500)
        return httpx.Response(200, json={"keys": self.keys})

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def ed_key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.generate()


@pytest.fixture
def server(ed_key: Ed25519PrivateKey) -> JWKSServer:
    srv = JWKSServer()
    srv.keys = [_ed_jwk(ed_key, "k1")]
    return srv


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def verifier(server: JWKSServer, clock: Clock) -> TokenVerifier:
    cache = JWKSCache(
        JWKS_URL,
        allowed_algorithms=ALGS,
        ttl_seconds=300,
        min_refresh_seconds=30,
        client=server.client(),
        clock=clock,
    )
    return TokenVerifier(jwks=cache, issuer=ISSUER, audience=AUDIENCE, algorithms=ALGS)


def _claims(**overrides: Any) -> dict[str, Any]:
    now = int(time.time())
    claims: dict[str, Any] = {
        "sub": "user_123",
        "iss": ISSUER,
        "aud": AUDIENCE,
        "iat": now,
        "exp": now + 300,
    }
    claims.update(overrides)
    return {k: v for k, v in claims.items() if v is not None}


def _sign(key: Any, alg: str = "EdDSA", kid: str | None = "k1", **claims: Any) -> str:
    headers = {"kid": kid} if kid is not None else {}
    return jwt.encode(_claims(**claims), key, algorithm=alg, headers=headers)


async def test_valid_token(verifier: TokenVerifier, ed_key: Ed25519PrivateKey) -> None:
    result = await verifier.verify(_sign(ed_key))
    assert result.subject == "user_123"
    assert result.key_id == "k1"


async def test_rsa_token(verifier: TokenVerifier, server: JWKSServer, clock: Clock) -> None:
    rsa = generate_private_key(public_exponent=65537, key_size=2048)
    server.keys.append(_rsa_jwk(rsa, "r1"))
    assert (await verifier.verify(_sign(rsa, "RS256", "r1"))).subject == "user_123"


def _unsigned(kid: str = "k1") -> str:
    import base64

    def b64(obj: dict[str, Any]) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()

    return f"{b64({'alg': 'none', 'typ': 'JWT', 'kid': kid})}.{b64(_claims())}."


@pytest.mark.parametrize(
    ("make_token", "message"),
    [
        (lambda key: _unsigned(), "unsupported token algorithm"),
        (
            lambda key: jwt.encode(
                _claims(),
                "a-shared-secret-long-enough-0123",
                algorithm="HS256",
                headers={"kid": "k1"},
            ),
            "unsupported token algorithm",
        ),
        (lambda key: _sign(key, exp=int(time.time()) - 3600), "token expired"),
        (lambda key: _sign(key, aud="https://someone-else"), "Invalid or expired token"),
        (lambda key: _sign(key, iss="https://evil.example"), "Invalid or expired token"),
        (lambda key: _sign(key, kid=None), "token has no key id"),
        (lambda key: _sign(key, kid="unknown"), "unknown signing key"),
        (lambda key: _sign(Ed25519PrivateKey.generate()), "Invalid or expired token"),
        (lambda key: _sign(key, sub=None), "Invalid or expired token"),
        (lambda key: _sign(key, exp=None), "Invalid or expired token"),
        (lambda key: _sign(key, nbf=int(time.time()) + 3600), "Invalid or expired token"),
        (lambda key: "not-a-jwt", "malformed token"),
        (lambda key: "", "Invalid or expired token"),
    ],
    ids=[
        "alg-none",
        "hs256",
        "expired",
        "wrong-aud",
        "wrong-iss",
        "no-kid",
        "unknown-kid",
        "bad-signature",
        "no-sub",
        "no-exp",
        "not-yet-valid",
        "garbage",
        "empty",
    ],
)
async def test_rejected_tokens(
    verifier: TokenVerifier,
    ed_key: Ed25519PrivateKey,
    make_token: Callable[[Ed25519PrivateKey], str],
    message: str,
) -> None:
    with pytest.raises(TokenError) as info:
        await verifier.verify(make_token(ed_key))
    assert info.value.status == 401
    assert info.value.detail == message


async def test_algorithm_must_match_key(verifier: TokenVerifier, server: JWKSServer) -> None:
    rsa = generate_private_key(public_exponent=65537, key_size=2048)
    server.keys.append(_rsa_jwk(rsa, "r1"))
    # Header says EdDSA but kid points at the RSA key.
    token = _sign(Ed25519PrivateKey.generate(), "EdDSA", "r1")
    with pytest.raises(TokenError, match="does not match"):
        await verifier.verify(token)


async def test_symmetric_keys_in_jwks_are_ignored(
    verifier: TokenVerifier, server: JWKSServer
) -> None:
    secret = "a-shared-secret-long-enough-0123"  # noqa: S105 (test value)
    server.keys = [{"kty": "oct", "kid": "k1", "k": "c2VjcmV0", "alg": "HS256"}]
    token = jwt.encode(_claims(), secret, algorithm="HS256", headers={"kid": "k1"})
    with pytest.raises(TokenError):
        await verifier.verify(token)


async def test_key_rotation_refetches_once(
    verifier: TokenVerifier, server: JWKSServer, clock: Clock, ed_key: Ed25519PrivateKey
) -> None:
    await verifier.verify(_sign(ed_key))
    assert server.hits == 1

    new_key = Ed25519PrivateKey.generate()
    server.keys.append(_ed_jwk(new_key, "k2"))
    # Within the refresh gap, an unknown kid does not trigger another fetch.
    with pytest.raises(TokenError, match="unknown signing key"):
        await verifier.verify(_sign(new_key, kid="k2"))
    assert server.hits == 1

    clock.now += 31
    assert (await verifier.verify(_sign(new_key, kid="k2"))).key_id == "k2"
    assert server.hits == 2

    # A removed key stops working once the cache expires.
    server.keys = [_ed_jwk(new_key, "k2")]
    clock.now += 301
    with pytest.raises(TokenError, match="unknown signing key"):
        await verifier.verify(_sign(ed_key))


async def test_jwks_outage_fails_closed(
    verifier: TokenVerifier, server: JWKSServer, clock: Clock, ed_key: Ed25519PrivateKey
) -> None:
    server.fail = True
    with pytest.raises(JWKSUnavailableError):
        await verifier.verify(_sign(ed_key))

    server.fail = False
    await verifier.verify(_sign(ed_key))
    # Stale cache plus outage: keys are dropped, not trusted indefinitely.
    server.fail = True
    clock.now += 301
    with pytest.raises(JWKSUnavailableError):
        await verifier.verify(_sign(ed_key))


def test_verifier_refuses_symmetric_config(server: JWKSServer) -> None:
    cache = JWKSCache(JWKS_URL, allowed_algorithms=["HS256"], client=server.client())
    with pytest.raises(ValueError, match="asymmetric"):
        TokenVerifier(jwks=cache, issuer=ISSUER, audience=AUDIENCE, algorithms=["HS256"])
