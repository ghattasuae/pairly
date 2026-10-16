# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""The approver's view of a sensitive message while the application is blind.

A rejection or an offer waits for a person's approval (``messaging.graph``). Its
approval payload is the exact email, with the candidate's name and address, and the
approval binds the hash of that exact payload. While the application is blind (blind
mode applies and its one reveal state, ``pairly_hire.reveal``, is not set), the
approver must not learn who the candidate is, so the approval detail shows the same
message with the known name, email and phone replaced by labelled placeholders
(``blindtext.mask_identity``). Nothing else changes: the stored payload, its hash, the
decision and the email that is sent all carry the real values.

Registered for the ``hire.message`` approval type (``register``, called by the plugin
module). Blindness is read at view time, so the approver sees the real message once
the application is revealed. Fail closed: a message or application that cannot be read
hides the whole payload (``pairly_core.approvals.views``).
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from pairly_core.approvals.models import Approval
from pairly_core.approvals.views import ApprovalView, register_view_filter
from pairly_core.crypto.types import unseal
from pairly_hire.blindtext import MASK_NOTE, identity_sources, mask_values
from pairly_hire.messaging.graph import APPROVAL_TYPE, ENTITY
from pairly_hire.messaging.models import HireMessage
from pairly_hire.pipeline.hooks import application_blind
from pairly_hire.pipeline.records import load_record

MASKED_KEYS = ("subject", "text", "recipient")


async def message_approval_view(session: AsyncSession, approval: Approval) -> ApprovalView | None:
    if approval.entity_type != ENTITY or not approval.entity_id:
        raise LookupError("approval without its message")
    message = await session.scalar(
        select(HireMessage).where(HireMessage.id == uuid.UUID(approval.entity_id))
    )
    if message is None:
        raise LookupError("message not found")
    record = await load_record(session, message.application_id)
    blind = await application_blind(session, record.app.id)
    if not record.identity_hidden(blind):
        return None
    candidate = {
        "full_name": unseal(record.candidate, "full_name"),
        "email": unseal(record.candidate, "email"),
        "phone": unseal(record.candidate, "phone"),
    }
    sources = identity_sources(record.contact, candidate)
    return ApprovalView(
        payload=mask_values(dict(approval.payload or {}), sources, MASKED_KEYS),
        context=mask_values(dict(approval.context or {}), sources, MASKED_KEYS),
        masked=("name", "email", "phone"),
        note=MASK_NOTE,
    )


def register() -> None:
    register_view_filter(APPROVAL_TYPE, message_approval_view)
