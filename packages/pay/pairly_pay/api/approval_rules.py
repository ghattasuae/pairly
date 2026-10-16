# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""The approval rules Pay uses, read only (WP4.7).

* ``GET /approval-rules``  for each Pay approval type (invoice, payment run, bank-detail
  change): the version in force (the newest published), every version, and the bundled
  default that becomes version 1 the first time the type is used. Needs
  ``pay.invoice.read``.

Publishing a new version stays with the engine (``POST /v1/approval-rules/{type}``,
``core.org.manage`` with a second factor). Old versions are kept, and every approval
and decision record points at the version it used.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter
from pydantic import BaseModel

from pairly_core.approvals.rules import RULES_KIND
from pairly_core.identity import CurrentActor, OrgSession
from pairly_core.policy_versions import PublishedPolicy, list_policy_versions
from pairly_core.rbac import authorize
from pairly_pay.permissions import INVOICE_READ

router = APIRouter(tags=["pay"])


class PayRulesVersionOut(BaseModel):
    id: uuid.UUID
    version: int
    content: str
    content_hash: str
    created_at: datetime


class PayRulesOut(BaseModel):
    approval_type: str
    title: str
    # The newest published version (what new approvals use), or None before first use.
    active: PayRulesVersionOut | None
    # The bundled rules, published as version 1 the first time the type is used.
    default_content: str
    versions: list[PayRulesVersionOut]


class PayRulesList(BaseModel):
    items: list[PayRulesOut]


def _version(policy: PublishedPolicy) -> PayRulesVersionOut:
    return PayRulesVersionOut(
        id=policy.id,
        version=policy.version,
        content=policy.content,
        content_hash=policy.content_hash,
        created_at=policy.created_at,
    )


@router.get("/approval-rules", response_model=PayRulesList)
async def approval_rules(actor: CurrentActor, session: OrgSession) -> PayRulesList:
    authorize(actor, INVOICE_READ)
    from pairly_pay.plugin import plugin  # the graph specs (no import cycle at load)

    items: list[PayRulesOut] = []
    for spec in plugin.graphs:
        for kind in spec.approval_types:
            versions = await list_policy_versions(session, RULES_KIND, kind.key)
            ordered = sorted(versions, key=lambda p: p.version, reverse=True)
            items.append(
                PayRulesOut(
                    approval_type=kind.key,
                    title=kind.title,
                    active=_version(ordered[0]) if ordered else None,
                    default_content=kind.default_rules,
                    versions=[_version(p) for p in ordered],
                )
            )
    return PayRulesList(items=items)
