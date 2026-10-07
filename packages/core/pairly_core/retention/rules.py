# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Retention rules: which rows of which table become deletable after how long.

A rule names an org-scoped model, the timestamp column that ages, the number of days,
and optional extra conditions (for example "only delivered" rows). A row is due when
``column < now - days``. Rules only describe; ``pairly_core.retention.service`` counts.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Final

from sqlalchemy import ColumnElement

from pairly_core.idempotency import IdempotencyRecord
from pairly_core.retention.models import RetentionReport
from pairly_core.webhooks.models import (
    DeliveryStatus,
    InboundEvent,
    WebhookDelivery,
    WebhookOutbox,
)

RULE_KEY_RE: Final = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*){0,3}$")


class RetentionRuleError(ValueError):
    """An invalid or duplicate rule (a programming error)."""


@dataclass(frozen=True, slots=True)
class RetentionRule:
    key: str
    description: str
    model: Any  # an org-scoped ORM model
    column: str  # the timestamp column that ages
    days: int
    conditions: Callable[[], Sequence[ColumnElement[bool]]] = field(default=lambda: ())

    def __post_init__(self) -> None:
        if not RULE_KEY_RE.fullmatch(self.key) or len(self.key) > 100:
            raise RetentionRuleError(f"invalid retention rule key {self.key!r}")
        if not 0 <= self.days <= 36500:
            raise RetentionRuleError(f"rule {self.key!r}: days must be 0 to 36500")
        if not hasattr(self.model, "org_id") or not hasattr(self.model, self.column):
            raise RetentionRuleError(f"rule {self.key!r}: needs an org-scoped model and column")

    def cutoff(self, now: datetime) -> datetime:
        return now - timedelta(days=self.days)

    def due(self, now: datetime) -> list[ColumnElement[bool]]:
        """The WHERE clauses selecting the rows this rule would delete at ``now``."""
        column = getattr(self.model, self.column)
        return [column.is_not(None), column < self.cutoff(now), *self.conditions()]


CORE_RULES: tuple[RetentionRule, ...] = (
    RetentionRule(
        "idempotency_keys.expired",
        "Idempotency keys past their 24 hour lifetime",
        IdempotencyRecord,
        "expires_at",
        0,
    ),
    RetentionRule(
        "webhook_outbox.dispatched",
        "Webhook events already fanned out to deliveries",
        WebhookOutbox,
        "dispatched_at",
        30,
    ),
    RetentionRule(
        "webhook_deliveries.finished",
        "Webhook deliveries that succeeded, failed or were cancelled",
        WebhookDelivery,
        "completed_at",
        30,
        lambda: (WebhookDelivery.status != DeliveryStatus.PENDING.value,),
    ),
    RetentionRule(
        "inbound_events.seen",
        "Ids of inbound provider events already processed (deduplication)",
        InboundEvent,
        "received_at",
        90,
    ),
    RetentionRule(
        "retention_reports.old",
        "Earlier retention dry-run reports",
        RetentionReport,
        "created_at",
        90,
    ),
)


class RetentionRegistry:
    def __init__(self, rules: Iterable[RetentionRule] = CORE_RULES) -> None:
        self._rules: dict[str, RetentionRule] = {}
        for rule in rules:
            if rule.key in self._rules:
                raise RetentionRuleError(f"duplicate retention rule {rule.key!r}")
            self._rules[rule.key] = rule

    @property
    def rules(self) -> list[RetentionRule]:
        return sorted(self._rules.values(), key=lambda r: r.key)

    def __contains__(self, key: object) -> bool:
        return key in self._rules


def build_retention_registry(plugins: Iterable[Any] = ()) -> RetentionRegistry:
    """Core rules plus every plugin's optional ``retention_rules`` (keys ``<id>.*``)."""
    rules = list(CORE_RULES)
    for plugin in plugins:
        for rule in getattr(plugin, "retention_rules", ()) or ():
            if not isinstance(rule, RetentionRule):
                raise RetentionRuleError(f"plugin {plugin.id!r}: retention_rules must be rules")
            if not rule.key.startswith(f"{plugin.id}."):
                raise RetentionRuleError(
                    f"plugin {plugin.id!r}: rule {rule.key!r} is outside its namespace"
                )
            rules.append(rule)
    return RetentionRegistry(rules)
