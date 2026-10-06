# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Invitations, /v1/me/orgs, the TOTP step-up rule and the bootstrap CLI, on real Postgres.

Tenant fixtures: ``a`` is an approver in org A, ``b`` is an admin in org B.
"""

from __future__ import annotations

import asyncio
import dataclasses
import re
import secrets
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Annotated, Any

import httpx
import pytest
from fastapi import Depends, FastAPI
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from pairly_api import admin as admin_cli
from pairly_api.main import create_app
from pairly_core.identity import current_actor
from pairly_core.invitations import hash_token
from pairly_core.plugins import WorkflowPlugin
from pairly_core.rbac import Actor, ActorKind, require
from pairly_hire.plugin import plugin as hire_plugin
from pairly_pay.plugin import plugin as pay_plugin

from .conftest import DbUrls, TokenFactory, TwoTenants, make_settings

PLUGINS: list[WorkflowPlugin] = [hire_plugin, pay_plugin]
OTP = ["otp"]


@dataclasses.dataclass
class Live:
    app: FastAPI
    client: httpx.AsyncClient
    tokens: TokenFactory

    def auth(
        self,
        subject: str,
        *,
        org: uuid.UUID | None = None,
        amr: list[str] | None = None,
        email: str | None = None,
        verified: bool = True,
    ) -> dict[str, str]:
        claims: dict[str, Any] = {}
        if amr is not None:
            claims["amr"] = amr
        if email is not None:
            claims["email"] = email
            claims["email_verified"] = verified
        headers = {"Authorization": f"Bearer {self.tokens.token(subject, **claims)}"}
        if org is not None:
            headers["X-Pairly-Org"] = str(org)
        return headers

    async def invite(
        self, tenant_subject: str, org: uuid.UUID, email: str, role: str = "approver", **kw: Any
    ) -> httpx.Response:
        return await self.client.post(
            "/v1/invitations",
            json={"email": email, "role": role, **kw},
            headers=self.auth(tenant_subject, org=org, amr=OTP),
        )

    async def accept(self, token: str, subject: str, email: str, **kw: Any) -> httpx.Response:
        return await self.client.post(
            "/v1/invitations/accept",
            json={"token": token},
            headers=self.auth(subject, email=email, **kw),
        )


@pytest.fixture
async def live(migrated: DbUrls, tokens: TokenFactory) -> AsyncIterator[Live]:
    app = create_app(make_settings(migrated.app), plugins=PLUGINS)

    @app.get("/v1/test/approve")
    async def approve(
        actor: Annotated[Actor, Depends(require("pay.invoice.approve"))],
    ) -> dict[str, str]:
        return {"by": str(actor.id)}

    async with app.router.lifespan_context(app):
        app.state.pairly = dataclasses.replace(app.state.pairly, verifier=tokens.verifier())
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://api.test") as c:
            yield Live(app, c, tokens)


async def _purge_org_rows(owner_engine: AsyncEngine, org_id: uuid.UUID, *, drop_org: bool) -> None:
    """Remove what these tests add, so the tenant fixtures can drop their orgs."""
    async with owner_engine.begin() as conn:
        await conn.execute(text("SELECT set_config('app.org_id', :o, true)"), {"o": str(org_id)})
        await conn.execute(text("DELETE FROM invitations"))
        await conn.execute(
            text(
                "DELETE FROM memberships WHERE user_id NOT IN (SELECT id FROM users u "
                "WHERE u.auth_subject LIKE 'sub-a-%' OR u.auth_subject LIKE 'sub-b-%')"
            )
        )
        if drop_org:
            await conn.execute(text("DELETE FROM memberships"))
            await conn.execute(text("DELETE FROM organisations"))


@pytest.fixture
async def orgs(owner_engine: AsyncEngine, tenants: TwoTenants) -> AsyncIterator[TwoTenants]:
    yield tenants
    for org in (tenants.a.org_id, tenants.b.org_id):
        await _purge_org_rows(owner_engine, org, drop_org=False)


def _new_subject() -> str:
    return f"sub-new-{uuid.uuid4().hex[:12]}"


def _new_email() -> str:
    return f"new-{uuid.uuid4().hex[:10]}@example.test"


async def _actions(owner_engine: AsyncEngine, org: uuid.UUID) -> list[str]:
    async with owner_engine.begin() as conn:
        await conn.execute(text("SELECT set_config('app.org_id', :o, true)"), {"o": str(org)})
        rows = await conn.execute(text("SELECT action FROM audit_events ORDER BY seq"))
        return [r.action for r in rows]


# ------------------------------------------------------------------ happy path


async def test_invite_preview_accept(
    live: Live, orgs: TwoTenants, owner_engine: AsyncEngine
) -> None:
    b = orgs.b
    email, subject = _new_email(), _new_subject()
    created = await live.invite(b.subject, b.org_id, email.upper(), role="approver")
    assert created.status_code == 201, created.text
    body = created.json()
    token = body["token"]
    assert re.fullmatch(r"[A-Za-z0-9_-]{43}", token)
    assert body["invitation"]["email"] == email  # normalised
    assert body["invitation"]["status"] == "pending"

    # Only the hash is stored.
    async with owner_engine.begin() as conn:
        await conn.execute(text("SELECT set_config('app.org_id', :o, true)"), {"o": str(b.org_id)})
        stored = (await conn.execute(text("SELECT token_hash FROM invitations"))).scalars().all()
    assert stored == [hash_token(token)]
    assert token not in stored

    preview = await live.client.post("/v1/invitations/preview", json={"token": token})
    assert preview.status_code == 200, preview.text
    assert preview.json()["email"] == email
    assert preview.json()["role"] == "approver"
    assert preview.json()["org_name"] == "Org b"

    accepted = await live.accept(token, subject, email)
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["org_id"] == str(b.org_id)
    assert accepted.json()["role"] == "approver"

    me = await live.client.get("/v1/me", headers=live.auth(subject, org=b.org_id))
    assert me.status_code == 200, me.text
    assert me.json()["roles"] == [{"role": "approver", "workflow": None}]
    assert me.json()["email"] == email

    listed = await live.client.get("/v1/invitations", headers=live.auth(b.subject, org=b.org_id))
    assert listed.status_code == 200
    assert [i["status"] for i in listed.json()["items"]] == ["accepted"]

    actions = await _actions(owner_engine, b.org_id)
    assert actions[-3:] == [
        "core.invitation.created",
        "core.invitation.accepted",
        "core.membership.granted",
    ]


async def test_workflow_scoped_invitation(live: Live, orgs: TwoTenants) -> None:
    b = orgs.b
    email, subject = _new_email(), _new_subject()
    created = await live.invite(b.subject, b.org_id, email, role="approver", workflow="pay")
    assert created.status_code == 201, created.text
    assert (await live.accept(created.json()["token"], subject, email)).status_code == 200
    me = (await live.client.get("/v1/me", headers=live.auth(subject, org=b.org_id))).json()
    assert me["roles"] == [{"role": "approver", "workflow": "pay"}]
    assert "pay.invoice.approve" in me["permissions"]
    assert "core.approval.decide" not in me["permissions"]


# ------------------------------------------------------------------ token misuse


async def test_reused_token_is_refused(live: Live, orgs: TwoTenants) -> None:
    b = orgs.b
    email = _new_email()
    token = (await live.invite(b.subject, b.org_id, email)).json()["token"]
    first, second = _new_subject(), _new_subject()
    assert (await live.accept(token, first, email)).status_code == 200
    for subject in (first, second):
        again = await live.accept(token, subject, email)
        assert again.status_code == 404
        assert again.json()["code"] == "invitation_invalid"
    # The second person got nothing.
    me = await live.client.get("/v1/me", headers=live.auth(second, org=b.org_id))
    assert me.status_code == 403
    assert (
        await live.client.post("/v1/invitations/preview", json={"token": token})
    ).status_code == 404


async def test_concurrent_accepts_use_the_token_once(live: Live, orgs: TwoTenants) -> None:
    b = orgs.b
    email = _new_email()
    token = (await live.invite(b.subject, b.org_id, email)).json()["token"]
    results = await asyncio.gather(
        live.accept(token, _new_subject(), email), live.accept(token, _new_subject(), email)
    )
    assert sorted(r.status_code for r in results) == [200, 404]


async def test_expired_token_is_refused(
    live: Live, orgs: TwoTenants, owner_engine: AsyncEngine
) -> None:
    b = orgs.b
    email = _new_email()
    created = (await live.invite(b.subject, b.org_id, email)).json()
    async with owner_engine.begin() as conn:
        await conn.execute(text("SELECT set_config('app.org_id', :o, true)"), {"o": str(b.org_id)})
        await conn.execute(
            text(
                "UPDATE invitations SET created_at = now() - interval '8 days', "
                "expires_at = now() - interval '1 day' WHERE id = :i"
            ),
            {"i": created["invitation"]["id"]},
        )
    response = await live.accept(created["token"], _new_subject(), email)
    assert response.status_code == 404
    assert response.json()["code"] == "invitation_invalid"
    preview = await live.client.post("/v1/invitations/preview", json={"token": created["token"]})
    assert preview.status_code == 404
    listed = await live.client.get("/v1/invitations", headers=live.auth(b.subject, org=b.org_id))
    assert [i["status"] for i in listed.json()["items"]] == ["expired"]


async def test_wrong_or_unverified_email_is_refused(live: Live, orgs: TwoTenants) -> None:
    b = orgs.b
    email, subject = _new_email(), _new_subject()
    token = (await live.invite(b.subject, b.org_id, email)).json()["token"]

    wrong = await live.accept(token, subject, "someone-else@example.test")
    assert wrong.status_code == 403
    assert wrong.json()["code"] == "invitation_email_mismatch"

    unverified = await live.accept(token, subject, email, verified=False)
    assert unverified.status_code == 403
    assert unverified.json()["code"] == "email_not_verified"

    no_email = await live.client.post(
        "/v1/invitations/accept", json={"token": token}, headers=live.auth(subject)
    )
    assert no_email.status_code == 403
    assert no_email.json()["code"] == "email_not_verified"

    # None of that used the invitation up: the right person can still accept it.
    assert (await live.accept(token, subject, email.upper())).status_code == 200


async def test_revoked_token_is_refused(
    live: Live, orgs: TwoTenants, owner_engine: AsyncEngine
) -> None:
    b = orgs.b
    email = _new_email()
    created = (await live.invite(b.subject, b.org_id, email)).json()
    inv_id = created["invitation"]["id"]
    headers = live.auth(b.subject, org=b.org_id, amr=OTP)

    revoked = await live.client.delete(f"/v1/invitations/{inv_id}", headers=headers)
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["status"] == "revoked"
    assert (
        await live.client.delete(f"/v1/invitations/{inv_id}", headers=headers)
    ).status_code == 409

    response = await live.accept(created["token"], _new_subject(), email)
    assert response.status_code == 404
    assert "core.invitation.revoked" in await _actions(owner_engine, b.org_id)


async def test_terminal_invitation_cannot_be_reopened(
    live: Live, orgs: TwoTenants, app_engine: AsyncEngine
) -> None:
    """The database refuses to change an accepted or revoked invitation, even for the app."""
    b = orgs.b
    email = _new_email()
    created = (await live.invite(b.subject, b.org_id, email)).json()
    await live.client.delete(
        f"/v1/invitations/{created['invitation']['id']}",
        headers=live.auth(b.subject, org=b.org_id, amr=OTP),
    )
    async with app_engine.connect() as conn, conn.begin():
        await conn.execute(text("SELECT set_config('app.org_id', :o, true)"), {"o": str(b.org_id)})
        reopen = text("UPDATE invitations SET revoked_at = NULL, revoked_by = NULL")
        with pytest.raises(DBAPIError, match="already accepted or revoked"):
            await conn.execute(reopen)


@pytest.mark.parametrize("token", ["A" * 43, secrets.token_urlsafe(32)], ids=["constant", "random"])
async def test_forged_token_reads_and_joins_nothing(
    live: Live, orgs: TwoTenants, token: str
) -> None:
    a, b = orgs.a, orgs.b
    # A real pending invitation exists in B, so there is something to find.
    await live.invite(b.subject, b.org_id, _new_email())
    subject = _new_subject()
    assert (
        await live.client.post("/v1/invitations/preview", json={"token": token})
    ).status_code == 404
    response = await live.accept(token, subject, "x@example.test")
    assert response.status_code == 404
    for org in (a.org_id, b.org_id):
        me = await live.client.get("/v1/me", headers=live.auth(subject, org=org))
        assert me.status_code == 403
    orgs_seen = await live.client.get("/v1/me/orgs", headers=live.auth(subject))
    assert orgs_seen.json() == {"items": []}


@pytest.mark.parametrize("token", ["short", "A" * 44, "A" * 42 + "!"])
async def test_malformed_token_is_rejected_before_lookup(live: Live, token: str) -> None:
    response = await live.client.post("/v1/invitations/preview", json={"token": token})
    assert response.status_code == 422


async def test_lookup_function_returns_only_an_org_id(
    app_engine: AsyncEngine, live: Live, orgs: TwoTenants
) -> None:
    """The app role sees nothing without an org, and cannot use the owner-only policies."""
    b = orgs.b
    created = (await live.invite(b.subject, b.org_id, _new_email())).json()
    async with app_engine.begin() as conn:
        await conn.execute(
            text(
                "SELECT set_config('app.invitation_lookup', 'on', true), "
                "set_config('app.membership_lookup', 'on', true)"
            )
        )
        assert (await conn.execute(text("SELECT count(*) FROM invitations"))).scalar() == 0
        assert (await conn.execute(text("SELECT count(*) FROM memberships"))).scalar() == 0
        assert (await conn.execute(text("SELECT count(*) FROM organisations"))).scalar() == 0
        found = await conn.execute(
            text("SELECT invitation_org_id(:h)"), {"h": hash_token(created["token"])}
        )
        assert found.scalar() == b.org_id
        nothing = await conn.execute(text("SELECT invitation_org_id(:h)"), {"h": "0" * 64})
        assert nothing.scalar() is None
        bad = await conn.execute(text("SELECT invitation_org_id('not-a-hash')"))
        assert bad.scalar() is None


# ------------------------------------------------------------------ who may invite whom


async def test_cross_org_access_is_refused(live: Live, orgs: TwoTenants) -> None:
    a, b = orgs.a, orgs.b
    created = (await live.invite(b.subject, b.org_id, _new_email())).json()
    inv_id = created["invitation"]["id"]
    # a is not a member of B.
    for method, path in (("GET", "/v1/invitations"), ("DELETE", f"/v1/invitations/{inv_id}")):
        response = await live.client.request(
            method, path, headers=live.auth(a.subject, org=b.org_id, amr=OTP)
        )
        assert response.status_code == 403
    # a cannot manage users in A either (approver), and A cannot see B's invitation.
    response = await live.client.delete(
        f"/v1/invitations/{inv_id}", headers=live.auth(a.subject, org=a.org_id, amr=OTP)
    )
    assert response.status_code == 403
    # Accepting B's invitation never joins A.
    email, subject = _new_email(), _new_subject()
    token = (await live.invite(b.subject, b.org_id, email)).json()["token"]
    assert (await live.accept(token, subject, email)).json()["org_id"] == str(b.org_id)
    assert (
        await live.client.get("/v1/me", headers=live.auth(subject, org=a.org_id))
    ).status_code == 403


async def test_admin_cannot_see_other_orgs_invitation(
    live: Live, orgs: TwoTenants, owner_engine: AsyncEngine
) -> None:
    a, b = orgs.a, orgs.b
    # An invitation in A, created directly (A has no admin in the fixtures).
    inv_id = uuid.uuid4()
    async with owner_engine.begin() as conn:
        await conn.execute(text("SELECT set_config('app.org_id', :o, true)"), {"o": str(a.org_id)})
        await conn.execute(
            text(
                "INSERT INTO invitations (id, org_id, email, role_id, token_hash, expires_at) "
                "SELECT :i, :o, 'x@example.test', r.id, :h, now() + interval '1 day' "
                "FROM roles r WHERE r.key = 'admin' AND r.org_id IS NULL"
            ),
            {"i": inv_id, "o": a.org_id, "h": hash_token(secrets.token_urlsafe(32))},
        )
    headers = live.auth(b.subject, org=b.org_id, amr=OTP)
    assert (
        await live.client.delete(f"/v1/invitations/{inv_id}", headers=headers)
    ).status_code == 404
    listed = await live.client.get("/v1/invitations", headers=headers)
    assert str(inv_id) not in listed.text


@pytest.mark.parametrize(
    ("role", "workflow", "code"),
    [
        ("agent", None, "permission_denied"),
        ("superuser", None, "permission_denied"),
        ("approver", "payroll", "permission_denied"),
    ],
)
async def test_ungrantable_roles(
    live: Live, orgs: TwoTenants, role: str, workflow: str | None, code: str
) -> None:
    b = orgs.b
    body: dict[str, Any] = {"workflow": workflow} if workflow else {}
    response = await live.invite(b.subject, b.org_id, _new_email(), role=role, **body)
    assert response.status_code == 403
    assert response.json()["code"] == code


async def test_agent_role_refused_and_audited(
    live: Live, orgs: TwoTenants, owner_engine: AsyncEngine
) -> None:
    b = orgs.b
    response = await live.invite(b.subject, b.org_id, _new_email(), role="agent")
    assert response.status_code == 403
    assert (await _actions(owner_engine, b.org_id))[-1] == "core.authz.denied"


async def test_privilege_escalation_by_role_choice(live: Live, orgs: TwoTenants) -> None:
    """A user manager without admin's core powers cannot invite an admin or an auditor."""
    b = orgs.b
    manager = Actor(
        kind=ActorKind.USER,
        id=b.user_id,
        org_id=b.org_id,
        grants=(),
        permissions=frozenset({"core.users.manage", "core.users.read", "core.org.read"}),
        subject=b.subject,
        email="b@example.test",
        amr=frozenset({"otp"}),
    )
    live.app.dependency_overrides[current_actor] = lambda: manager
    try:
        for role in ("admin", "auditor", "approver"):
            response = await live.invite(b.subject, b.org_id, _new_email(), role=role)
            assert response.status_code == 403, role
            assert response.json()["code"] == "permission_denied"
    finally:
        live.app.dependency_overrides.clear()


async def test_cannot_invite_self_or_duplicate(live: Live, orgs: TwoTenants) -> None:
    b = orgs.b
    me = await live.invite(b.subject, b.org_id, "B@Example.test", role="approver")
    assert me.status_code == 400
    assert me.json()["code"] == "cannot_invite_self"

    email = _new_email()
    assert (await live.invite(b.subject, b.org_id, email)).status_code == 201
    duplicate = await live.invite(b.subject, b.org_id, email)
    assert duplicate.status_code == 409
    assert duplicate.json()["code"] == "invitation_pending"


async def test_existing_member_gets_conflict(live: Live, orgs: TwoTenants) -> None:
    b = orgs.b
    email, subject = _new_email(), _new_subject()
    token = (await live.invite(b.subject, b.org_id, email)).json()["token"]
    await live.accept(token, subject, email)
    again = await live.invite(b.subject, b.org_id, email)
    assert again.status_code == 409
    assert again.json()["code"] == "already_member"


async def test_inviting_needs_totp_but_listing_does_not(live: Live, orgs: TwoTenants) -> None:
    b = orgs.b
    for amr in (None, ["email"], ["sso"]):
        response = await live.client.post(
            "/v1/invitations",
            json={"email": _new_email(), "role": "read_only"},
            headers=live.auth(b.subject, org=b.org_id, amr=amr),
        )
        assert response.status_code == 403
        assert response.json()["code"] == "second_factor_required"
    listed = await live.client.get(
        "/v1/invitations", headers=live.auth(b.subject, org=b.org_id, amr=["email"])
    )
    assert listed.status_code == 200
    members = await live.client.get("/v1/members", headers=live.auth(b.subject, org=b.org_id))
    assert members.status_code == 200
    assert [m["email"] for m in members.json()["items"]] == ["b@example.test"]


# ------------------------------------------------------------------ /v1/me/orgs


async def test_me_orgs_lists_only_own_memberships(
    live: Live, orgs: TwoTenants, owner_engine: AsyncEngine
) -> None:
    a, b = orgs.a, orgs.b
    seen_a = await live.client.get("/v1/me/orgs", headers=live.auth(a.subject))
    assert seen_a.status_code == 200, seen_a.text
    assert [o["id"] for o in seen_a.json()["items"]] == [str(a.org_id)]
    seen_b = await live.client.get("/v1/me/orgs", headers=live.auth(b.subject))
    assert [o["id"] for o in seen_b.json()["items"]] == [str(b.org_id)]
    nobody = await live.client.get("/v1/me/orgs", headers=live.auth("nobody"))
    assert nobody.json() == {"items": []}

    # a joins B through an invitation: now both, in name order.
    token = (await live.invite(b.subject, b.org_id, "a@example.test", role="read_only")).json()[
        "token"
    ]
    assert (await live.accept(token, a.subject, "a@example.test")).status_code == 200
    both = await live.client.get("/v1/me/orgs", headers=live.auth(a.subject))
    assert [o["name"] for o in both.json()["items"]] == ["Org a", "Org b"]

    # A suspended organisation disappears.
    async with owner_engine.begin() as conn:
        await conn.execute(text("SELECT set_config('app.org_id', :o, true)"), {"o": str(b.org_id)})
        await conn.execute(text("UPDATE organisations SET status = 'suspended'"))
    try:
        after = await live.client.get("/v1/me/orgs", headers=live.auth(a.subject))
        assert [o["id"] for o in after.json()["items"]] == [str(a.org_id)]
    finally:
        async with owner_engine.begin() as conn:
            await conn.execute(
                text("SELECT set_config('app.org_id', :o, true)"), {"o": str(b.org_id)}
            )
            await conn.execute(text("UPDATE organisations SET status = 'active'"))


async def test_me_orgs_needs_a_token(live: Live) -> None:
    assert (await live.client.get("/v1/me/orgs")).status_code == 401


# ------------------------------------------------------------------ amr matrix


@pytest.mark.parametrize(
    ("amr", "status"),
    [
        (None, 403),
        (["email"], 403),
        (["sso"], 403),
        ([], 403),
        (["otp"], 200),
        (["email", "otp"], 200),
    ],
    ids=["missing", "magic-link", "sso", "empty", "totp", "email+totp"],
)
async def test_approve_needs_totp_sign_in(
    live: Live, orgs: TwoTenants, amr: list[str] | None, status: int
) -> None:
    """An SSO or magic link session without TOTP cannot approve (brief 6.1)."""
    a = orgs.a  # approver
    response = await live.client.get(
        "/v1/test/approve", headers=live.auth(a.subject, org=a.org_id, amr=amr)
    )
    assert response.status_code == status, response.text
    if status == 403:
        assert response.json()["code"] == "second_factor_required"


async def test_me_reports_second_factor(live: Live, orgs: TwoTenants) -> None:
    a = orgs.a
    without = (
        await live.client.get("/v1/me", headers=live.auth(a.subject, org=a.org_id, amr=["email"]))
    ).json()
    assert without["second_factor"] is False
    assert without["second_factor_permissions"] == ["core.approval.decide", "pay.invoice.approve"]
    with_otp = (
        await live.client.get("/v1/me", headers=live.auth(a.subject, org=a.org_id, amr=OTP))
    ).json()
    assert with_otp["second_factor"] is True


# ------------------------------------------------------------------ bootstrap CLI


@pytest.fixture
def created_orgs(migrated: DbUrls) -> Iterator[list[uuid.UUID]]:
    ids: list[uuid.UUID] = []
    yield ids

    async def purge() -> None:
        engine = create_async_engine(migrated.owner, pool_size=1, max_overflow=0)
        try:
            for org in ids:
                await _purge_org_rows(engine, org, drop_org=True)
        finally:
            await engine.dispose()

    asyncio.run(purge())


def test_cli_bootstrap_org(
    migrated: DbUrls,
    created_orgs: list[uuid.UUID],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv(admin_cli.OWNER_URL_ENV, migrated.owner)
    slug = f"acme-{uuid.uuid4().hex[:8]}"
    code = admin_cli.main(
        [
            "bootstrap-org",
            "--name",
            "Acme Ltd",
            "--slug",
            slug,
            "--admin-email",
            " Founder@Acme.Test ",
            "--web-url",
            "https://pairly.example",
            "--locale",
            "ar",
        ]
    )
    out = capsys.readouterr().out
    assert code == 0, out
    org_id = uuid.UUID(re.search(r"id: ([0-9a-f-]{36})", out).group(1))  # type: ignore[union-attr]
    created_orgs.append(org_id)
    link = re.search(r"(https://pairly\.example/ar/invite/[A-Za-z0-9_-]{43})", out)
    assert link is not None
    token = link.group(1).rsplit("/", 1)[1]

    async def check() -> tuple[Any, ...]:
        engine = create_async_engine(migrated.owner, pool_size=1, max_overflow=0)
        try:
            async with engine.begin() as conn:
                await conn.execute(
                    text("SELECT set_config('app.org_id', :o, true)"), {"o": str(org_id)}
                )
                org = (await conn.execute(text("SELECT name, slug FROM organisations"))).one()
                inv = (
                    await conn.execute(
                        text(
                            "SELECT i.email, r.key, i.token_hash, i.invited_by, "
                            "i.expires_at - i.created_at AS ttl "
                            "FROM invitations i JOIN roles r ON r.id = i.role_id"
                        )
                    )
                ).one()
                actions = (
                    (await conn.execute(text("SELECT action FROM audit_events ORDER BY seq")))
                    .scalars()
                    .all()
                )
                return tuple(org), tuple(inv), tuple(actions)
        finally:
            await engine.dispose()

    org, inv, actions = asyncio.run(check())
    assert org == ("Acme Ltd", slug)
    assert inv[:4] == ("founder@acme.test", "admin", hash_token(token), None)
    assert inv[4].days == 7
    assert actions == ("core.org.created", "core.invitation.created")

    # Same slug again: refused, nothing created.
    again = admin_cli.main(
        ["bootstrap-org", "--name", "Other", "--slug", slug, "--admin-email", "x@acme.test"]
    )
    assert again == 1
    bad_slug = admin_cli.main(
        ["bootstrap-org", "--name", "Other", "--slug", "Bad Slug", "--admin-email", "x@acme.test"]
    )
    assert bad_slug == 2


def test_cli_needs_owner_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(admin_cli.OWNER_URL_ENV, raising=False)
    assert (
        admin_cli.main(["bootstrap-org", "--name", "x", "--slug", "xx", "--admin-email", "a@b.c"])
        == 2
    )


async def test_bootstrapped_admin_can_accept_and_manage(
    live: Live, migrated: DbUrls, owner_engine: AsyncEngine
) -> None:
    slug = f"boot-{uuid.uuid4().hex[:8]}"
    result = await admin_cli.bootstrap(
        migrated.owner, name="Boot Co", slug=slug, admin_email="boss@boot.test"
    )
    try:
        subject = _new_subject()
        accepted = await live.accept(result.token, subject, "boss@boot.test")
        assert accepted.status_code == 200, accepted.text
        assert accepted.json()["org_id"] == str(result.org.id)
        me = await live.client.get("/v1/me", headers=live.auth(subject, org=result.org.id, amr=OTP))
        assert me.json()["roles"] == [{"role": "admin", "workflow": None}]
        invite = await live.invite(subject, result.org.id, _new_email(), role="approver")
        assert invite.status_code == 201, invite.text
    finally:
        await _purge_org_rows(owner_engine, result.org.id, drop_org=True)


def test_accept_link_format() -> None:
    link = admin_cli.accept_link("http://localhost:3000/", "en", "a" * 43)
    assert link == "http://localhost:3000/en/invite/" + "a" * 43
