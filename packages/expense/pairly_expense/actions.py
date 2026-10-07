# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""The workflow's only final action: mark an approved note reimbursable.

It runs inside the executor (``pairly_core.approvals.executor``), after the executor
re-checked from the database that people with ``expense.note.approve`` and a second
factor approved exactly this payload, and in the same transaction that marks the
approval executed. It changes state only: no money moves (brief hard rule 3).

It checks the note itself too: the note must be the one the approved payload names,
waiting for this very approval of this run. Anything else refuses (fail closed).
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from pairly_core.approvals.executor import EXECUTOR_ACTOR_ID, ExecutorRefusedError
from pairly_core.audit import ActorType, append_audit
from pairly_core.workflow.spec import FinalActionCall
from pairly_expense.models import NoteStatus
from pairly_expense.notes import ENTITY_TYPE, WORKFLOW, NoteMissingError, locked_note

APPROVED_ACTION = "expense.note.approved"


async def mark_reimbursable(session: AsyncSession, call: FinalActionCall) -> Mapping[str, Any]:
    try:
        note_id = uuid.UUID(str(call.payload.get("note_id")))
        note = await locked_note(session, note_id)
    except (ValueError, NoteMissingError):
        raise ExecutorRefusedError("note_not_found") from None
    if note.approval_id != call.approval_id or note.run_id != call.run_id:
        raise ExecutorRefusedError("note_not_of_this_approval")
    if note.status != NoteStatus.AWAITING_APPROVAL.value:
        raise ExecutorRefusedError("note_not_awaiting_approval")
    now = datetime.now(UTC)
    note.status = NoteStatus.APPROVED.value
    note.reimbursable = True
    note.decided_at = note.updated_at = now
    await session.flush()
    await append_audit(
        session,
        action=APPROVED_ACTION,
        actor_type=ActorType.SYSTEM,
        actor_id=EXECUTOR_ACTOR_ID,
        entity_type=ENTITY_TYPE,
        entity_id=note.id,
        workflow=WORKFLOW,
        payload={
            "approval_id": str(call.approval_id),
            "run_id": str(call.run_id),
            "approvers": [str(a) for a in call.approver_ids],
            "amount": str(note.amount) if note.amount is not None else None,
            "currency": note.currency,
        },
    )
    return {"note_id": str(note.id), "status": note.status, "reimbursable": True}
