# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Proofly Technology
"""Typed shapes of the responses the helpers return (mirroring the OpenAPI schemas).

They are ``TypedDict``s: the JSON stays a plain ``dict`` at run time (nothing to
convert, new fields pass through), and type checkers know the keys. Timestamps and ids
are strings, as on the wire. ``tests/test_openapi_conformance.py`` checks these keys
against ``packages/shared-ts/openapi/v1.json``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, TypedDict

ItemStatus = Literal["pending", "accepted", "duplicate", "rejected", "failed"]
BatchStatus = Literal["processing", "completed"]


class BatchItem(TypedDict):
    id: str
    position: int
    source: Literal["file", "url"]
    url: str | None
    filename: str | None
    status: ItemStatus
    reason: str | None
    document_id: str | None
    entity_type: str | None
    entity_id: str | None
    completed_at: str | None


class Batch(TypedDict):
    id: str
    workflow: str
    status: BatchStatus
    item_count: int
    metadata: dict[str, Any]
    counts: dict[str, int]
    items: list[BatchItem]
    created_at: str
    completed_at: str | None


class Step(TypedDict):
    id: str
    position: int
    name: str
    mode: str
    status: str
    assignee_ids: list[str]
    escalation_ids: list[str]
    activated_at: str | None
    due_at: str | None
    escalated_at: str | None
    completed_at: str | None


class Approval(TypedDict):
    id: str
    workflow: str
    approval_type: str
    title: str
    status: str
    run_id: str
    entity_type: str | None
    entity_id: str | None
    created_at: str
    updated_at: str
    decided_at: str | None
    sla_state: Literal["on_track", "due_soon", "overdue"] | None
    current_step: Step | None
    guardrail_findings: int


class ApprovalPage(TypedDict):
    items: list[Approval]
    next_cursor: str | None


class Proposal(TypedDict):
    id: str
    workflow: str | None
    entity_type: str
    entity_id: str
    kind: Literal["note", "action"]
    action: str | None
    body: str
    proposed_by_type: str
    proposed_by_id: str
    created_at: str


class ProposalPage(TypedDict):
    items: list[Proposal]
    next_cursor: str | None


@dataclass(frozen=True, slots=True)
class Submission:
    """What ``documents.submit`` returns: the job (a one-item batch) and its item.

    ``job_id`` is the batch id: poll ``documents.get_batch(job_id)`` or wait for the
    ``batch.completed`` webhook. ``entity_type`` and ``entity_id`` name what the workflow
    made of the document (Pairly Pay: ``pay.invoice`` and the invoice id); its decisions
    arrive later as ``decision.recorded`` and ``approval.decided`` webhooks.
    """

    job_id: str
    status: str
    item: BatchItem
    batch: Batch

    @property
    def document_id(self) -> str | None:
        return self.item["document_id"]

    @property
    def entity_type(self) -> str | None:
        return self.item["entity_type"]

    @property
    def entity_id(self) -> str | None:
        return self.item["entity_id"]

    @classmethod
    def of(cls, batch: Batch) -> Submission:
        return cls(
            job_id=batch["id"],
            status=batch["items"][0]["status"],
            item=batch["items"][0],
            batch=batch,
        )
