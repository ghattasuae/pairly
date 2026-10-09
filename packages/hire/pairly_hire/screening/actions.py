# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""The screening's one final action, ``shortlist``, run by the approvals executor.

The executor has already re-checked the approval in the database (a person with
``hire.screening.approve`` approved this exact payload, with a second factor). The
action marks the screening ``shortlisted`` and lifts blind mode: from now on reviewers
see the candidate's name and contact details (brief 3.3.3, "until the shortlist
stage"). It never changes the application's pipeline stage or messages anyone; the
pipeline (WP2.5) reads the screening's status.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Final

from sqlalchemy.ext.asyncio import AsyncSession

from pairly_core.approvals.executor import EXECUTOR_ACTOR_ID, ExecutorRefusedError
from pairly_core.audit import ActorType, append_audit
from pairly_core.workflow.spec import FinalActionCall
from pairly_hire.screening.models import ScreeningStatus
from pairly_hire.screening.service import (
    SCREENING_ENTITY,
    WORKFLOW,
    ScreeningMissingError,
    locked_screening,
)

SHORTLIST: Final = "shortlist"


async def shortlist(session: AsyncSession, call: FinalActionCall) -> Mapping[str, Any]:
    try:
        screening_id = uuid.UUID(str(call.payload["screening_id"]))
        row = await locked_screening(session, screening_id)
    except (KeyError, ValueError, ScreeningMissingError):
        raise ExecutorRefusedError("screening_not_found") from None
    if row.run_id != call.run_id or row.approval_id != call.approval_id:
        raise ExecutorRefusedError("screening_not_of_this_approval")
    if row.status != ScreeningStatus.AWAITING_REVIEW.value:
        raise ExecutorRefusedError("screening_not_awaiting_review")
    now = datetime.now(UTC)
    row.status = ScreeningStatus.SHORTLISTED.value
    row.blind = False
    row.revealed_at = row.decided_at = row.updated_at = now
    await session.flush()
    await append_audit(
        session,
        action="hire.screening.shortlisted",
        actor_type=ActorType.SYSTEM,
        actor_id=EXECUTOR_ACTOR_ID,
        entity_type=SCREENING_ENTITY,
        entity_id=row.id,
        workflow=WORKFLOW,
        payload={
            "run_id": str(call.run_id),
            "approval_id": str(call.approval_id),
            "application_id": str(row.application_id),
            "approvers": [str(a) for a in call.approver_ids],
        },
    )
    return {"screening_id": str(row.id), "status": row.status}
