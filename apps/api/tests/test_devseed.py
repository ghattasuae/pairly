# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""The development approval seed (``pairly_api.devseed``) on Postgres.

* It publishes no rules of its own: the first approval in a dev org publishes Pay's
  bundled ``pay.invoice`` rules as version 1, exactly as Pay's first invoice would,
  and the approval is routed by them (the standard chain, one step).
* It refuses to run without ``PAIRLY_DEVSEED=1`` or outside development and test.
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from pairly_api.devseed import main, plugin_approval_type, seed_approval
from pairly_core.plugins import discover_plugins
from pairly_pay.approvals import INVOICE_RULES_V1

from .conftest import DbUrls
from .pay_support import owner_rows, seed_pay_world


def test_the_approval_type_is_the_pay_plugins() -> None:
    kind = plugin_approval_type(discover_plugins().values())
    assert kind.key == "pay.invoice"
    assert kind.permission == "pay.invoice.approve"
    assert kind.default_rules == INVOICE_RULES_V1
    with pytest.raises(SystemExit):
        plugin_approval_type(discover_plugins().values(), "no.such_type")


async def test_seed_publishes_pays_default_rules_as_version_1(
    migrated: DbUrls, owner_engine: AsyncEngine
) -> None:
    world = await seed_pay_world(owner_engine, {"admin": (("admin", None),)})
    rows = await owner_rows(
        owner_engine, world.org_id, "SELECT email FROM users WHERE id = :u",
        {"u": world.id("admin")},
    )  # fmt: skip
    email = rows[0].email
    for title in ("First", "Second"):
        result = await seed_approval(
            migrated.owner,
            org_slug=world.slug,
            approver_email=email,
            title=title,
            overdue=False,
            link=None,
            decision=False,
        )
        assert result["approval_id"]
    versions = await owner_rows(
        owner_engine,
        world.org_id,
        "SELECT version, content FROM policy_versions "
        "WHERE org_id = :o AND name = 'pay.invoice' ORDER BY version",
        {"o": world.org_id},
    )
    assert [(v.version, v.content) for v in versions] == [(1, INVOICE_RULES_V1)]
    approvals = await owner_rows(
        owner_engine,
        world.org_id,
        "SELECT chain_name, status FROM approvals WHERE org_id = :o",
        {"o": world.org_id},
    )
    assert {(a.chain_name, a.status) for a in approvals} == {("standard", "pending")}


@pytest.mark.parametrize(
    ("env", "flag"), [("production", "1"), ("staging", "1"), ("development", None)]
)
def test_refused_outside_development(
    monkeypatch: pytest.MonkeyPatch, env: str, flag: str | None
) -> None:
    monkeypatch.setenv("PAIRLY_ENV", env)
    if flag is None:
        monkeypatch.delenv("PAIRLY_DEVSEED", raising=False)
    else:
        monkeypatch.setenv("PAIRLY_DEVSEED", flag)
    assert main(["approval", "--org-slug", "x", "--approver-email", "a@b.test"]) == 2
