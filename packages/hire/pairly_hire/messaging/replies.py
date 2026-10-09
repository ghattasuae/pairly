# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Candidate replies: the interface the candidate portal (WP2.9) writes through.

``record_reply`` stores a message from the candidate on their application
(``hire_messages``, direction ``inbound``, status ``received``, purpose ``reply``): the
text is encrypted, it appears in the profile timeline and the conversation, and it is
audited without its text. The caller (the portal, after it verified the candidate's
session, as ``ActorType.CANDIDATE``) passes the application id; nothing here trusts the
text: it is shown as plain text and never sent to a model.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Final

from sqlalchemy.ext.asyncio import AsyncSession

from pairly_core.audit import ActorType, append_audit
from pairly_core.crypto.types import seal
from pairly_core.errors import BadRequestError
from pairly_hire.messaging.models import HireMessage, MessagePurpose, MessageStatus
from pairly_hire.pipeline.records import load_record

WORKFLOW: Final = "hire"
PORTAL_ACTOR: Final = "pairly.hire.portal"
MAX_REPLY: Final = 8000
MAX_SUBJECT: Final = 200


async def record_reply(
    session: AsyncSession,
    application_id: uuid.UUID,
    *,
    text: str,
    subject: str | None = None,
    actor_id: str = PORTAL_ACTOR,
    actor_type: ActorType = ActorType.SYSTEM,
) -> HireMessage:
    """Store a candidate's reply on their application. Raises ``BadRequestError`` on an
    empty or too long text, ``NotFoundError`` for an unknown application."""
    body = text.strip()
    if not body or len(body) > MAX_REPLY:
        raise BadRequestError(f"a reply has 1 to {MAX_REPLY} characters")
    record = await load_record(session, application_id)
    now = datetime.now(UTC)
    message = HireMessage(
        id=uuid.uuid4(),
        org_id=record.app.org_id,
        application_id=record.app.id,
        candidate_id=record.candidate.id,
        direction="inbound",
        template_key=None,
        locale=record.locale,
        purpose=MessagePurpose.REPLY.value,
        sensitive=False,
        status=MessageStatus.RECEIVED.value,
        ai_fields=[],
        created_by_type=actor_type.value,
        created_by_id=actor_id,
        created_at=now,
        updated_at=now,
    )
    sealed_vars = seal(HireMessage, "variables", {}, org_id=message.org_id, row_id=message.id)
    sealed_text = seal(
        HireMessage,
        "rendered",
        {"subject": (subject or "").strip()[:MAX_SUBJECT] or None, "text": body},
        org_id=message.org_id,
        row_id=message.id,
    )
    setattr(message, "variables", sealed_vars)  # noqa: B010 (typed column, sealed value)
    setattr(message, "rendered", sealed_text)  # noqa: B010
    session.add(message)
    await session.flush()
    await append_audit(
        session,
        action="hire.message.received",
        actor_type=actor_type,
        actor_id=actor_id,
        entity_type="hire.message",
        entity_id=message.id,
        workflow=WORKFLOW,
        payload={"application_id": str(record.app.id), "chars": len(body)},
    )
    return message
