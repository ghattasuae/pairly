# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""The workflow's policies as code: version 1 of each, and the seed hook that publishes them.

Three versioned YAML documents (``pairly_core.policy_versions``), all named
``expense.note``:

* ``rules``: the expense policy (receipt over 200 AED, category limits, weekends),
  evaluated by the deterministic rules engine (``pairly_core.policy.rules``).
* ``risk_policy``: how risk signals combine (``pairly_core.risk.combiner``).
* ``approval_rules``: who approves (``pairly_core.approvals.rules``).

``ensure_policies`` publishes version 1 of any of them the organisation does not have
yet, and never touches a policy that already has a version (an admin's edit wins). The
seed hook calls it, and so does the graph before it evaluates anything, the same way
the approvals engine publishes a type's default rules on first use.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from importlib.resources import files
from typing import Any, Final

from sqlalchemy.ext.asyncio import AsyncSession

from pairly_core.approvals.rules import RULES_KIND as APPROVAL_RULES_KIND
from pairly_core.approvals.rules import rules_validator
from pairly_core.audit import ActorType
from pairly_core.policy.rules import RULES_KIND, rule_set_validator
from pairly_core.policy_versions import PublishedPolicy, latest_policy, publish_policy_version
from pairly_core.risk.combiner import POLICY_KIND as RISK_KIND
from pairly_core.risk.combiner import risk_policy_validator

POLICY_NAME: Final = "expense.note"
APPROVAL_TYPE: Final = "expense.note"


def _text(name: str) -> str:
    return files("pairly_expense").joinpath("policy", name).read_text(encoding="utf-8")


RULES_V1: Final = _text("rules.yaml")
RISK_V1: Final = _text("risk.yaml")
APPROVAL_RULES_V1: Final = _text("approval_rules.yaml")


@dataclass(frozen=True, slots=True)
class Defaults:
    kind: str
    name: str
    content: str


DEFAULTS: Final = (
    Defaults(RULES_KIND, POLICY_NAME, RULES_V1),
    Defaults(RISK_KIND, POLICY_NAME, RISK_V1),
    Defaults(APPROVAL_RULES_KIND, APPROVAL_TYPE, APPROVAL_RULES_V1),
)


def _validator(kind: str, name: str) -> Callable[[str], Any]:
    validate: Callable[[str], Any]
    if kind == RULES_KIND:
        validate = rule_set_validator(name)
    elif kind == RISK_KIND:
        validate = risk_policy_validator(name)
    else:
        validate = rules_validator(name)
    return validate


async def ensure_policies(
    session: AsyncSession, *, actor_type: ActorType, actor_id: str
) -> list[PublishedPolicy]:
    """Publish version 1 of each policy the organisation lacks. Returns what it published."""
    published: list[PublishedPolicy] = []
    for item in DEFAULTS:
        if await latest_policy(session, item.kind, item.name) is not None:
            continue
        validate = _validator(item.kind, item.name)
        published.append(
            await publish_policy_version(
                session,
                kind=item.kind,
                name=item.name,
                content=item.content,
                validate=validate,
                actor_type=actor_type,
                actor_id=actor_id,
            )
        )
    return published
