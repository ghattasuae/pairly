# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Retention rules: what becomes deletable after how long, and how it is deleted.

Every rule says how its data goes (``mode``):

* ``rows``: technical rows of one org-scoped table (``model``) whose timestamp
  ``column`` is older than ``days`` (plus optional ``conditions``) are deleted.
* ``subjects``: a person or a company and everything about them (a candidate, say), one
  subject at a time, through the plugin's own proven erasure (``SubjectRule``): the
  database part in one transaction, then the stored files, retried until they are gone.
* ``report``: counted and shown only; another job of the plugin removes the data by its
  own setting (the pre-employment checklist files, say).

``days`` is the default period; an organisation changes it (``configurable_days``) and
switches each rule on or off (``pairly_core.retention.settings``). Rules only describe;
``pairly_core.retention.service`` lists, schedules and deletes.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Final

from sqlalchemy import ColumnElement

from pairly_core.idempotency import IdempotencyRecord
from pairly_core.retention.models import RetentionReport
from pairly_core.webhooks.models import (
    DeliveryStatus,
    InboundEvent,
    WebhookDelivery,
    WebhookOutbox,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from pairly_core.ingestion.storage import ObjectStore
    from pairly_core.rbac import Actor

RULE_KEY_RE: Final = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*){0,3}$")
SUBJECT_TYPE_RE: Final = re.compile(r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$")
MAX_DAYS: Final = 36500


class RetentionRuleError(ValueError):
    """An invalid or duplicate rule (a programming error)."""


class DeleteMode(StrEnum):
    ROWS = "rows"
    SUBJECTS = "subjects"
    REPORT = "report"


@dataclass(frozen=True, slots=True)
class DueSubject:
    """A subject a rule would delete: since when it is due, and a label for the preview
    list that names no one (a reference, never a name)."""

    subject_id: uuid.UUID
    due_at: datetime
    label: str | None = None


# ``due(session, now, days)``: the subjects due at ``now`` under a period of ``days``.
DueFn = Callable[["AsyncSession", datetime, int], Awaitable[Sequence[DueSubject]]]
# ``erase(session, subject_id, actor_id)``: the database part, in the caller's transaction;
# returns what it cleared (counts). Raises to refuse (the transaction is rolled back).
EraseFn = Callable[["AsyncSession", uuid.UUID, str], Awaitable[Mapping[str, int]]]
# ``finish(session, store, subject_id)``: after the database part committed, the stored
# files. Returns None when they are all gone, else an error code (retried next run).
FinishFn = Callable[["AsyncSession", "ObjectStore | None", uuid.UUID], Awaitable[str | None]]


@dataclass(frozen=True, slots=True)
class PeriodHook:
    """A rule whose period a plugin keeps in its own settings (Hire keeps the months of
    rejected candidates, which talent rediscovery reads too). The retention settings
    show and change it through ``get`` and ``set`` instead of a number of days."""

    unit: str  # "months" or "days"
    default: int
    minimum: int
    maximum: int
    get: Callable[[AsyncSession], Awaitable[int]]
    set: Callable[[AsyncSession, int, Actor], Awaitable[None]]

    def __post_init__(self) -> None:
        if self.unit not in ("months", "days"):
            raise RetentionRuleError(f"invalid period unit {self.unit!r}")
        if not 0 <= self.minimum <= self.default <= self.maximum:
            raise RetentionRuleError("period default must lie within its range")


@dataclass(frozen=True, slots=True)
class SubjectRule:
    subject_type: str  # "hire.candidate"
    due: DueFn
    erase: EraseFn
    finish: FinishFn | None = None

    def __post_init__(self) -> None:
        if not SUBJECT_TYPE_RE.fullmatch(self.subject_type) or len(self.subject_type) > 64:
            raise RetentionRuleError(f"invalid subject type {self.subject_type!r}")


@dataclass(frozen=True, slots=True)
class RetentionRule:
    key: str
    description: str
    model: Any  # an org-scoped ORM model (``rows`` and ``report``; ``subjects``: optional)
    column: str  # the timestamp column that ages
    days: int
    conditions: Callable[[], Sequence[ColumnElement[bool]]] = field(default=lambda: ())
    mode: DeleteMode = DeleteMode.ROWS
    subjects: SubjectRule | None = None
    workflow: str = "core"
    default_enabled: bool = True
    configurable_days: bool = True
    # Set when the plugin keeps the period itself (``days`` is then not used).
    period: PeriodHook | None = None

    def __post_init__(self) -> None:
        if not RULE_KEY_RE.fullmatch(self.key) or len(self.key) > 100:
            raise RetentionRuleError(f"invalid retention rule key {self.key!r}")
        if not 0 <= self.days <= MAX_DAYS:
            raise RetentionRuleError(f"rule {self.key!r}: days must be 0 to {MAX_DAYS}")
        if (self.mode is DeleteMode.SUBJECTS) != (self.subjects is not None):
            raise RetentionRuleError(f"rule {self.key!r}: a subjects rule needs its SubjectRule")
        if self.mode is not DeleteMode.SUBJECTS and (
            not hasattr(self.model, "org_id") or not hasattr(self.model, self.column)
        ):
            raise RetentionRuleError(f"rule {self.key!r}: needs an org-scoped model and column")

    def cutoff(self, now: datetime, days: int | None = None) -> datetime:
        return now - timedelta(days=self.days if days is None else days)

    def due(self, now: datetime, days: int | None = None) -> list[ColumnElement[bool]]:
        """The WHERE clauses selecting the rows this rule would delete at ``now``."""
        column = getattr(self.model, self.column)
        return [column.is_not(None), column < self.cutoff(now, days), *self.conditions()]


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
        "Earlier retention reports",
        RetentionReport,
        "created_at",
        90,
    ),
)


class RetentionRegistry:
    def __init__(
        self, rules: Iterable[RetentionRule] = CORE_RULES, hold_subjects: Iterable[str] = ()
    ) -> None:
        self._rules: dict[str, RetentionRule] = {}
        for rule in rules:
            if rule.key in self._rules:
                raise RetentionRuleError(f"duplicate retention rule {rule.key!r}")
            self._rules[rule.key] = rule
        subjects = {r.subjects.subject_type for r in self._rules.values() if r.subjects}
        for kind in hold_subjects:
            if not SUBJECT_TYPE_RE.fullmatch(kind):
                raise RetentionRuleError(f"invalid hold subject type {kind!r}")
            subjects.add(kind)
        self._hold_subjects = frozenset(subjects)

    @property
    def rules(self) -> list[RetentionRule]:
        return sorted(self._rules.values(), key=lambda r: r.key)

    @property
    def hold_subjects(self) -> frozenset[str]:
        """The kinds of subject a legal hold can be placed on (candidates, vendors)."""
        return self._hold_subjects

    def get(self, key: str) -> RetentionRule | None:
        return self._rules.get(key)

    def __contains__(self, key: object) -> bool:
        return key in self._rules


def build_retention_registry(plugins: Iterable[Any] = ()) -> RetentionRegistry:
    """Core rules plus every plugin's optional ``retention_rules`` (keys ``<id>.*``) and
    ``retention_hold_subjects`` (subject types ``<id>.*`` a legal hold may name)."""
    rules = list(CORE_RULES)
    holds: list[str] = []
    for plugin in plugins:
        for rule in getattr(plugin, "retention_rules", ()) or ():
            if not isinstance(rule, RetentionRule):
                raise RetentionRuleError(f"plugin {plugin.id!r}: retention_rules must be rules")
            if not rule.key.startswith(f"{plugin.id}."):
                raise RetentionRuleError(
                    f"plugin {plugin.id!r}: rule {rule.key!r} is outside its namespace"
                )
            if rule.subjects is not None and not rule.subjects.subject_type.startswith(
                f"{plugin.id}."
            ):
                raise RetentionRuleError(
                    f"plugin {plugin.id!r}: subject type of {rule.key!r} is outside its namespace"
                )
            rules.append(rule)
        for kind in getattr(plugin, "retention_hold_subjects", ()) or ():
            if not str(kind).startswith(f"{plugin.id}."):
                raise RetentionRuleError(
                    f"plugin {plugin.id!r}: hold subject {kind!r} is outside its namespace"
                )
            holds.append(str(kind))
    return RetentionRegistry(rules, holds)
