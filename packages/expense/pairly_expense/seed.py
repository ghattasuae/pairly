# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""``pairly-admin seed expense-demo --org <slug>``: an organisation ready for the demo.

Idempotent, in one transaction (the CLI's), as the owner database role:

1. Publish version 1 of the expense policy, risk policy and approval rules
   (``ensure_policies``), unless the organisation already has a version.
2. Invite three synthetic people, each limited to the expense workflow:
   a submitter, an approver and a finance controller. Invitations already pending or
   accepted are left alone. The submitter is an ``employee`` (no core permission;
   here: submit, read their own notes, contest a decision).
3. Import the recorded model responses for the synthetic receipts in ``synth/expense``
   (``data/recordings.json``), so the demo runs offline in ``recorded`` mode.
"""

from __future__ import annotations

from importlib.resources import files
from typing import Final

from sqlalchemy.ext.asyncio import AsyncSession

from pairly_core.audit import ActorType
from pairly_core.invitations import invite_as_system
from pairly_core.llm.bundle import import_recordings, parse_bundle
from pairly_core.plugins import SeedContext, SeededInvitation, SeedResult
from pairly_expense.notes import WORKFLOW
from pairly_expense.policies import ensure_policies

SEED_ACTOR: Final = "pairly-admin.seed.expense-demo"
# (who, built-in role). Every membership is limited to the expense workflow.
DEMO_PEOPLE: Final = (
    ("submitter", "employee"),
    ("approver", "approver"),
    ("finance", "finance_controller"),
)


def recordings_text() -> str:
    return files("pairly_expense").joinpath("data", "recordings.json").read_text(encoding="utf-8")


def demo_email(slug: str, who: str) -> str:
    return f"{slug}-expense-{who}@example.test"


async def seed_expense_demo(session: AsyncSession, ctx: SeedContext) -> SeedResult:
    lines: list[str] = []
    published = await ensure_policies(session, actor_type=ActorType.SYSTEM, actor_id=SEED_ACTOR)
    for policy in published:
        lines.append(f"published {policy.kind} {policy.name} version {policy.version}")
    if not published:
        lines.append("policies: already published, left as they are")

    invitations: list[SeededInvitation] = []
    for who, role in DEMO_PEOPLE:
        email = demo_email(ctx.org_slug, who)
        created = await invite_as_system(
            session, ctx.registry, email=email, role=role, workflow=WORKFLOW, actor_id=SEED_ACTOR
        )
        if created is None:
            lines.append(f"{email}: already invited or a member")
            continue
        invitations.append(SeededInvitation(email, role, WORKFLOW, created.token))

    report = await import_recordings(session, ctx.org_id, parse_bundle(recordings_text()))
    lines.append(
        f"recorded model responses: {report.imported} imported, {report.skipped} already there"
    )
    return SeedResult(lines=tuple(lines), invitations=tuple(invitations))
