# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Shared fixtures for API tests.

Database tests need two URLs for a throwaway Postgres 16 database:

* ``PAIRLY_TEST_DATABASE_URL``: the non-owner application role (``pairly_app``).
* ``PAIRLY_TEST_DATABASE_URL_OWNER``: the owner role that runs migrations and seeds.

When either is unset, database tests are skipped with a message saying so.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import jwt
import pytest
from alembic import command
from alembic.config import Config
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from jwt.algorithms import OKPAlgorithm
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from pairly_core.auth import JWKSCache, TokenVerifier
from pairly_core.config import Environment, Settings, to_asyncpg_url

ISSUER = "https://auth.test.pairly"
AUDIENCE = "https://api.test.pairly"
JWKS_URL = "https://auth.test.pairly/api/auth/jwks"
KID = "test-key"

APP_URL_ENV = "PAIRLY_TEST_DATABASE_URL"
OWNER_URL_ENV = "PAIRLY_TEST_DATABASE_URL_OWNER"
ALEMBIC_INI = Path(__file__).resolve().parents[1] / "alembic.ini"


# ------------------------------------------------------------------ JWT


@dataclass
class TokenFactory:
    key: Ed25519PrivateKey = field(default_factory=Ed25519PrivateKey.generate)

    def jwks(self) -> dict[str, Any]:
        jwk = json.loads(OKPAlgorithm.to_jwk(self.key.public_key()))
        return {"keys": [jwk | {"kid": KID, "alg": "EdDSA", "use": "sig"}]}

    def token(self, subject: str, **claims: Any) -> str:
        now = int(time.time())
        payload = {"sub": subject, "iss": ISSUER, "aud": AUDIENCE, "iat": now, "exp": now + 300}
        payload.update(claims)
        return jwt.encode(payload, self.key, algorithm="EdDSA", headers={"kid": KID})

    def verifier(self) -> TokenVerifier:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=self.jwks())

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        cache = JWKSCache(JWKS_URL, allowed_algorithms=["EdDSA"], client=client)
        return TokenVerifier(jwks=cache, issuer=ISSUER, audience=AUDIENCE, algorithms=["EdDSA"])


@pytest.fixture
def tokens() -> TokenFactory:
    return TokenFactory()


def make_settings(database_url: str = "postgresql://app:pw@127.0.0.1:1/none") -> Settings:
    return Settings(
        env=Environment.TEST,
        database_url=SecretStr(database_url),
        auth_jwks_url=JWKS_URL,
        auth_issuer=ISSUER,
        auth_audience=AUDIENCE,
        cors_origins=["https://app.test.pairly"],
    )


# ------------------------------------------------------------------ database


@dataclass(frozen=True)
class DbUrls:
    app: str
    owner: str


@pytest.fixture(scope="session")
def db_urls() -> DbUrls:
    app, owner = os.environ.get(APP_URL_ENV), os.environ.get(OWNER_URL_ENV)
    if not app or not owner:
        pytest.skip(
            f"database tests need {APP_URL_ENV} (app role) and {OWNER_URL_ENV} (owner role)"
        )
    return DbUrls(app=to_asyncpg_url(app), owner=to_asyncpg_url(owner))


@pytest.fixture(scope="session")
def migrated(db_urls: DbUrls) -> DbUrls:
    app_role = make_url(db_urls.app).username or "pairly_app"
    previous = os.environ.get("PAIRLY_DB_APP_ROLE")
    os.environ["PAIRLY_DB_APP_ROLE"] = app_role
    try:
        config = Config(str(ALEMBIC_INI))
        config.attributes["database_url"] = db_urls.owner
        command.upgrade(config, "head")
    finally:
        if previous is None:
            os.environ.pop("PAIRLY_DB_APP_ROLE", None)
        else:
            os.environ["PAIRLY_DB_APP_ROLE"] = previous
    return db_urls


@pytest.fixture
async def owner_engine(migrated: DbUrls) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(migrated.owner, pool_size=1, max_overflow=0)
    yield engine
    await engine.dispose()


@pytest.fixture
async def app_engine(migrated: DbUrls) -> AsyncIterator[AsyncEngine]:
    # One pooled connection, so tests also prove the tenant setting never leaks
    # from one transaction to the next on a reused connection.
    engine = create_async_engine(migrated.app, pool_size=1, max_overflow=0)
    yield engine
    await engine.dispose()


@dataclass(frozen=True)
class Tenant:
    org_id: uuid.UUID
    user_id: uuid.UUID
    subject: str
    membership_id: uuid.UUID
    custom_role_id: uuid.UUID


@dataclass(frozen=True)
class TwoTenants:
    a: Tenant
    b: Tenant


# Wave C tenant tables a request may leave behind, children first (fixed names, safe to
# format into SQL). Not listed, because nothing may delete their rows: templates and
# api_keys (guard triggers), decision_records, decision_replays and appeals (append
# only, like audit_events). An organisation that holds such rows is never dropped.
TENANT_TABLES = (
    # WP1.11: the webhook outbox (every upload emits document.received), deliveries,
    # endpoints, inbound dedupe and stored idempotency keys.
    "webhook_deliveries",
    "webhook_outbox",
    "webhook_endpoints",
    "inbound_events",
    "idempotency_keys",
    # WP1.9: notifications (they reference templates, approvals and users), preferences,
    # Slack links and integrations.
    "notifications",
    "notification_preferences",
    "slack_interactions",
    "slack_link_requests",
    "slack_user_links",
    "integrations",
    # WP1.10 / WP1.10b job and rate limit rows.
    "compliance_exports",
    "audit_verifications",
    "audit_denial_limits",
    # WP1.12.
    "feature_flags",
    "retention_reports",
)


async def _seed_tenant(conn: Any, label: str, role_key: str) -> Tenant:
    """Insert one org with one user, one custom role and one membership.

    Runs with the tenant setting applied, so it also works for a non-superuser owner
    under forced RLS.
    """
    org_id, user_id, membership_id, custom_role_id = (uuid.uuid4() for _ in range(4))
    subject = f"sub-{label}-{uuid.uuid4().hex[:8]}"
    await conn.execute(
        text(
            "SELECT set_config('app.org_id', :org, true), "
            "set_config('app.auth_subject', :sub, true)"
        ),
        {"org": str(org_id), "sub": subject},
    )
    await conn.execute(
        text("INSERT INTO organisations (id, name, slug) VALUES (:id, :name, :slug)"),
        {"id": org_id, "name": f"Org {label}", "slug": f"org-{label}-{org_id.hex[:8]}"},
    )
    await conn.execute(
        text("INSERT INTO users (id, auth_subject, email) VALUES (:id, :sub, :email)"),
        {"id": user_id, "sub": subject, "email": f"{label}@example.test"},
    )
    await conn.execute(
        text("INSERT INTO roles (id, org_id, key, name) VALUES (:id, :org, 'custom', 'Custom')"),
        {"id": custom_role_id, "org": org_id},
    )
    await conn.execute(
        text(
            "INSERT INTO memberships (id, org_id, user_id, role_id) "
            "SELECT :id, :org, :user, r.id FROM roles r WHERE r.key = :role AND r.org_id IS NULL"
        ),
        {"id": membership_id, "org": org_id, "user": user_id, "role": role_key},
    )
    return Tenant(org_id, user_id, subject, membership_id, custom_role_id)


async def _drop_tenant(conn: Any, tenant: Tenant) -> None:
    await conn.execute(
        text(
            "SELECT set_config('app.org_id', :org, true), "
            "set_config('app.auth_subject', :sub, true)"
        ),
        {"org": str(tenant.org_id), "sub": tenant.subject},
    )
    for table in TENANT_TABLES:
        await conn.execute(
            text(f"DELETE FROM {table} WHERE org_id = :o"),  # noqa: S608  # nosec B608
            {"o": tenant.org_id},
        )
    await conn.execute(text("DELETE FROM memberships WHERE org_id = :o"), {"o": tenant.org_id})
    await conn.execute(text("DELETE FROM roles WHERE org_id = :o"), {"o": tenant.org_id})
    await conn.execute(text("DELETE FROM users WHERE id = :u"), {"u": tenant.user_id})
    await conn.execute(text("DELETE FROM organisations WHERE id = :o"), {"o": tenant.org_id})


@pytest.fixture
async def tenants(owner_engine: AsyncEngine) -> AsyncIterator[TwoTenants]:
    async with owner_engine.begin() as conn:
        a = await _seed_tenant(conn, "a", "approver")
    async with owner_engine.begin() as conn:
        b = await _seed_tenant(conn, "b", "admin")
    yield TwoTenants(a=a, b=b)
    for tenant in (a, b):
        async with owner_engine.begin() as conn:
            await _drop_tenant(conn, tenant)


@pytest.fixture
async def make_org(owner_engine: AsyncEngine) -> AsyncIterator[Any]:
    """Factory for new organisations (``policy_support.Org``) with one member of ``role``.

    Their documents are removed afterwards, since the stalled-document sweeper in other
    tests scans every organisation. The organisations themselves stay (API keys and
    policy documents cannot be deleted); they are unique and never reused.
    """
    from .policy_support import Org

    made: list[Org] = []

    async def make(role: str = "admin") -> Org:
        async with owner_engine.begin() as conn:
            tenant = await _seed_tenant(conn, f"k{uuid.uuid4().hex[:6]}", role)
        made.append(Org(tenant))
        return made[-1]

    yield make
    for org in made:
        async with owner_engine.begin() as conn:
            await conn.execute(
                text("SELECT set_config('app.org_id', :o, true)"), {"o": str(org.id)}
            )
            # Policy files stay: policy documents (append-only) reference them.
            await conn.execute(
                text(
                    "DELETE FROM documents WHERE org_id = :o AND id NOT IN "
                    "(SELECT document_id FROM policy_documents WHERE org_id = :o)"
                ),
                {"o": org.id},
            )
