# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Erasing a candidate's personal data, after their deletion request (brief 6.9).

Only ever a person's action: an admin with ``hire.settings.manage`` (second factor)
opens the candidate's open deletion request and confirms it with a written reason
(``POST /v1/hire/data-requests/{id}/erase``). Nothing starts an erasure on a schedule or
from an agent (hard rule 1: people delete data, agents never do); the scheduled job
``hire_erasure_objects`` only finishes one a person started.

Everything that names or describes the candidate goes. What stays is what the audit and
decision trail legally needs: ids, references, hashes, timestamps and outcomes (stages,
statuses, scores, decisions).

Two steps, so the database and the object store can never disagree in the unsafe way:

1. ``erase_for_request`` (one transaction): every row about the candidate is cleared
   and the request records ``erasure_started_at``; it stays open.
2. After that commit, ``finish_erasure`` deletes the stored files (original bytes and
   the parsed text) from the object store, then completes the request with the admin's
   reason as its outcome. When the store fails, the request stays open with
   ``erasure_error`` for the admin and the job retries it (fail closed: a request is
   never completed while a file may remain).

Erased in step 1:

* the candidate: name, address (replaced by one that cannot receive mail, with a blind
  index nobody can produce from a real address), phone, location, profile link, name
  blind indexes; ``erased_at`` is set. Talent pool consent ends.
* applications: form answers, contact details as sent, duplicate hints, upload token.
  Consent version, time and language stay.
* documents (the CV and every other file of any workflow linked to the applications,
  document requests or screenings): file name and parse warnings cleared, status
  ``erased``; SHA-256 and size stay. The search chunks of the candidate and of the
  documents are deleted, and so are the extraction values (``erase_extraction_values``)
  and the inbound email's sender and subject when a file came by email. A document
  another candidate's application also references is left alone (it is theirs too).
* messages to and from the candidate: subject, text and template values (direction,
  time, template key and content hash stay), and the notifications queued or sent for
  them (content and address; queued ones are suppressed).
* notes about the candidate (text becomes ``[erased]``), stage move reasons.
* screenings: the evidence quotes, the profile read from the CV, the guardrail report,
  knockout answers and flag explanations; the criterion scores, overall score and
  decision ids stay. Approvals of the candidate's screenings and messages keep only
  ``{"erased": true}`` as context and payload, and the workflow checkpoints of those
  runs are deleted.
* interviews: the candidate's time zone, meeting link and link nonce; the briefings and
  invitations sent to the panel and the candidate (notification content).
* interview feedback: the notes of every scorecard about the candidate (cleared; the
  ratings and recommendations stay), and the text of their debrief summaries (content
  emptied) and the reason of the decision taken on them (``[erased]``; the decision,
  who and when stay).
* offers: the compensation (emptied) and the candidate's note on their answer; the
  status, dates and the answer stay. Their approvals and decision records are erased
  like the others (by entity id).
* references: the referee's name and address (replaced), their answers (removed), the
  kept reference summaries (content emptied); their links and sessions end. Status and
  times stay.
* portal sign-in links and sessions (deleted), the candidate's notes on their data
  requests, the reasons of their review requests (appeals), their self-identification
  answers (deleted).
* decision records about the candidate: the encrypted ``sensitive`` part
  (``pairly_core.decisions.service.erase_sensitive``, audited with the record ids).
  ``verify_record`` then answers ``erased``.
* model calls about the candidate's documents, screenings and messages: request and
  response bodies (hashes, tokens and cost stay).

Audit events are never changed (the chain is append-only and holds ids, not content).
"""

from __future__ import annotations

import contextlib
import hashlib
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final

import structlog
from sqlalchemy import delete, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from pairly_core.approvals.models import Approval
from pairly_core.audit import ActorType, append_audit, audit_actor_action
from pairly_core.crypto.blind_index import blind_index
from pairly_core.crypto.types import seal, unseal
from pairly_core.db import set_tenant
from pairly_core.decisions.models import Appeal, DecisionRecord
from pairly_core.decisions.service import erase_sensitive
from pairly_core.errors import ConflictError, NotFoundError
from pairly_core.extraction.models import Extraction
from pairly_core.ingestion.models import Document, DocumentStatus, InboundEmail
from pairly_core.ingestion.storage import ObjectStore, StorageError, blob_key
from pairly_core.jobs.registry import JobContext
from pairly_core.models.llm import ModelCall
from pairly_core.notifications.models import Notification
from pairly_core.rbac import Actor
from pairly_core.retrieval.models import SearchChunk
from pairly_core.workflow.checkpoint import CHECKPOINT_SCHEMA
from pairly_core.workflow.models import WorkflowRun
from pairly_hire.interviewing.models import HireDebrief, HireScorecard
from pairly_hire.messaging.models import HireCandidatePrefs, HireMessage
from pairly_hire.models import HireApplication, HireCandidate
from pairly_hire.offers.models import HireOffer
from pairly_hire.pipeline.models import HireNote, HireStageMove
from pairly_hire.pipeline.rediscovery import remove_candidate
from pairly_hire.portal.models import (
    DataRequestKind,
    DataRequestStatus,
    HireDataRequest,
    HireDocumentRequest,
    HirePortalSession,
    HirePortalToken,
)
from pairly_hire.references.models import (
    HireReferenceLink,
    HireReferenceRequest,
    HireReferenceSession,
    HireReferenceSummary,
)
from pairly_hire.scheduling.models import HireInterview
from pairly_hire.screening.models import HireScreening
from pairly_hire.screening.service import read_details
from pairly_hire.selfid.models import HireSelfIdentification

WORKFLOW: Final = "hire"
ENTITY: Final = "hire.data_request"
ERASED: Final = "hire.data_request.erased"
ERASURE_STARTED: Final = "hire.data_request.erasure_started"
ERASURE_FAILED: Final = "hire.data_request.erasure_failed"
ERASED_NAME: Final = "Erased candidate"
ERASED_TEXT: Final = "[erased]"
ERASED_MARK: Final[dict[str, Any]] = {"erased": True}
ERASED_TIMEZONE: Final = "UTC"
JOB_ACTOR: Final = "pairly.hire.erasure"

log = structlog.get_logger("pairly_hire.erasure")
# The workflow checkpoints of the candidate's runs (fixed SQL, the schema is a constant).
_CHECKPOINT_DELETES: Final = tuple(
    text(f"DELETE FROM {CHECKPOINT_SCHEMA}.{table} WHERE thread_id = ANY(:t)")  # noqa: S608
    for table in ("checkpoint_writes", "checkpoint_blobs", "checkpoints")
)
# Error codes shown to the admin while files remain (``hire_data_requests.erasure_error``).
STORE_UNAVAILABLE: Final = "object_store_unavailable"
STORE_FAILED: Final = "object_store_delete_failed"


def erased_email(candidate_id: uuid.UUID) -> str:
    return f"erased-{candidate_id.hex}@erased.invalid"


def erased_bidx(candidate_id: uuid.UUID) -> str:
    """A unique placeholder blind index: no real address hashes to it (it is not keyed)."""
    return hashlib.sha256(f"pairly.erased:{candidate_id}".encode()).hexdigest()


def _sealed(model: Any, column: str, value: Any, *, org_id: uuid.UUID, row_id: object) -> Any:
    return seal(model, column, value, org_id=org_id, row_id=row_id)


def _uuids(value: Any) -> set[uuid.UUID]:
    """Every UUID written as a string anywhere in a JSON value (model call ids)."""
    found: set[uuid.UUID] = set()
    if isinstance(value, Mapping):
        for item in value.values():
            found |= _uuids(item)
    elif isinstance(value, list | tuple):
        for item in value:
            found |= _uuids(item)
    elif isinstance(value, str) and len(value) == 36:
        with contextlib.suppress(ValueError):
            found.add(uuid.UUID(value))
    return found


@dataclass
class _Scope:
    """Everything that belongs to one candidate."""

    org_id: uuid.UUID
    candidate_id: uuid.UUID
    app_ids: set[uuid.UUID] = field(default_factory=set)
    document_ids: set[uuid.UUID] = field(default_factory=set)
    shared_document_ids: set[uuid.UUID] = field(default_factory=set)
    message_ids: set[uuid.UUID] = field(default_factory=set)
    note_ids: set[uuid.UUID] = field(default_factory=set)
    interview_ids: set[uuid.UUID] = field(default_factory=set)
    screening_ids: set[uuid.UUID] = field(default_factory=set)
    data_request_ids: set[uuid.UUID] = field(default_factory=set)
    document_request_ids: set[uuid.UUID] = field(default_factory=set)
    appeal_ids: set[uuid.UUID] = field(default_factory=set)
    reference_ids: set[uuid.UUID] = field(default_factory=set)
    offer_ids: set[uuid.UUID] = field(default_factory=set)
    extraction_ids: set[uuid.UUID] = field(default_factory=set)
    decision_ids: set[str] = field(default_factory=set)
    model_call_ids: set[uuid.UUID] = field(default_factory=set)
    email_bidxs: set[str] = field(default_factory=set)

    def entity_ids(self) -> list[str]:
        ids: set[uuid.UUID] = {
            self.candidate_id,
            *self.app_ids,
            *self.document_ids,
            *self.message_ids,
            *self.note_ids,
            *self.interview_ids,
            *self.screening_ids,
            *self.data_request_ids,
            *self.document_request_ids,
            *self.appeal_ids,
            *self.extraction_ids,
            *self.reference_ids,
            *self.offer_ids,
        }
        return sorted(str(i) for i in ids)


@dataclass(frozen=True, slots=True)
class ErasureCounts:
    applications: int = 0
    documents: int = 0
    documents_shared: int = 0
    messages: int = 0
    notes: int = 0
    interviews: int = 0
    feedback: int = 0
    references: int = 0
    offers: int = 0
    screenings: int = 0
    notifications: int = 0
    decisions: int = 0
    model_calls: int = 0
    approvals: int = 0
    search_chunks: int = 0

    def as_payload(self) -> dict[str, int]:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}


# ------------------------------------------------------------------ scope


async def _documents_of(session: AsyncSession, scope: _Scope) -> None:
    apps = list(
        await session.scalars(
            select(HireApplication).where(HireApplication.candidate_id == scope.candidate_id)
        )
    )
    docs: set[uuid.UUID] = set()
    for app in apps:
        scope.app_ids.add(app.id)
        if app.cv_document_id is not None:
            docs.add(app.cv_document_id)
        for value in (app.files or {}).values():
            try:
                docs.add(uuid.UUID(str(value)))
            except ValueError:
                continue
    for req in await session.scalars(
        select(HireDocumentRequest).where(HireDocumentRequest.candidate_id == scope.candidate_id)
    ):
        scope.document_request_ids.add(req.id)
        if req.document_id is not None:
            docs.add(req.document_id)
    if scope.app_ids:
        for row in await session.scalars(
            select(HireScreening).where(HireScreening.application_id.in_(scope.app_ids))
        ):
            scope.screening_ids.add(row.id)
            docs.add(row.document_id)
    if not docs:
        return
    # A file another candidate's application also references is theirs too: kept.
    shared = await session.execute(
        text(
            "SELECT a.cv_document_id AS id FROM hire_applications a "
            "WHERE a.candidate_id <> :cid AND a.cv_document_id = ANY(:docs) "
            "UNION SELECT f.value::uuid FROM hire_applications a, jsonb_each_text(a.files) f "
            "WHERE a.candidate_id <> :cid AND f.value = ANY(:texts) "
            "UNION SELECT r.document_id FROM hire_document_requests r "
            "WHERE r.candidate_id <> :cid AND r.document_id = ANY(:docs)"
        ),
        {"cid": scope.candidate_id, "docs": list(docs), "texts": [str(d) for d in docs]},
    )
    scope.shared_document_ids = {uuid.UUID(str(r)) for r in shared.scalars() if r is not None}
    scope.document_ids = docs - scope.shared_document_ids


async def _scope(session: AsyncSession, candidate: HireCandidate) -> _Scope:
    scope = _Scope(org_id=candidate.org_id, candidate_id=candidate.id)
    scope.email_bidxs.add(candidate.email_bidx)
    await _documents_of(session, scope)
    cid = scope.candidate_id
    scope.message_ids = set(
        await session.scalars(select(HireMessage.id).where(HireMessage.candidate_id == cid))
    )
    scope.interview_ids = set(
        await session.scalars(select(HireInterview.id).where(HireInterview.candidate_id == cid))
    )
    scope.data_request_ids = set(
        await session.scalars(select(HireDataRequest.id).where(HireDataRequest.candidate_id == cid))
    )
    if scope.app_ids:
        scope.note_ids = set(
            await session.scalars(
                select(HireNote.id).where(HireNote.application_id.in_(scope.app_ids))
            )
        )
        for app in await session.scalars(
            select(HireApplication).where(HireApplication.id.in_(scope.app_ids))
        ):
            contact = unseal(app, "contact")
            email = contact.get("email") if isinstance(contact, Mapping) else None
            if isinstance(email, str):
                found = blind_index("email", email, scope.org_id)
                if found:
                    scope.email_bidxs.add(found)
    if scope.document_ids:
        scope.extraction_ids = set(
            await session.scalars(
                select(Extraction.id).where(Extraction.document_id.in_(scope.document_ids))
            )
        )
    for screening in await _screenings(session, scope):
        if screening.extraction_id is not None:
            scope.extraction_ids.add(screening.extraction_id)
        scope.model_call_ids |= _uuids(screening.model_info)
        scope.decision_ids |= {d for d in screening.decision_ids if isinstance(d, str)}
        if screening.score_decision_id:
            scope.decision_ids.add(screening.score_decision_id)
    scope.appeal_ids = set(
        await session.scalars(
            select(Appeal.id).where(Appeal.requester_id == f"hire_candidate:{cid}")
        )
    )
    scope.reference_ids = set(
        await session.scalars(
            select(HireReferenceRequest.id).where(HireReferenceRequest.candidate_id == cid)
        )
    )
    scope.offer_ids = set(
        await session.scalars(select(HireOffer.id).where(HireOffer.candidate_id == cid))
    )
    return scope


async def _screenings(session: AsyncSession, scope: _Scope) -> list[HireScreening]:
    if not scope.screening_ids:
        return []
    return list(
        await session.scalars(
            select(HireScreening).where(HireScreening.id.in_(scope.screening_ids)).with_for_update()
        )
    )


# ------------------------------------------------------------------ hire tables


async def _erase_candidate(session: AsyncSession, candidate: HireCandidate, now: datetime) -> None:
    org, cid = candidate.org_id, candidate.id
    candidate.email = _sealed(HireCandidate, "email", erased_email(cid), org_id=org, row_id=cid)
    candidate.full_name = _sealed(HireCandidate, "full_name", ERASED_NAME, org_id=org, row_id=cid)
    candidate.email_bidx = erased_bidx(cid)
    candidate.name_bidx = []
    candidate.phone = None
    candidate.phone_bidx = None
    candidate.location = None
    candidate.profile_url = None
    candidate.erased_at = candidate.erased_at or now
    candidate.updated_at = now
    prefs = await session.scalar(
        select(HireCandidatePrefs).where(HireCandidatePrefs.candidate_id == cid).with_for_update()
    )
    if prefs is not None:
        prefs.talent_pool = False
        prefs.talent_pool_at = None
        prefs.updated_at = now


async def _erase_applications(session: AsyncSession, scope: _Scope, now: datetime) -> int:
    count = 0
    for app in await session.scalars(
        select(HireApplication).where(HireApplication.id.in_(scope.app_ids)).with_for_update()
    ):
        for column in ("answers", "contact"):
            setattr(
                app, column, _sealed(HireApplication, column, {}, org_id=app.org_id, row_id=app.id)
            )
        app.duplicate_signals = []
        app.upload_token_hash = None
        app.upload_token_expires_at = None
        app.updated_at = now
        count += 1
    if scope.app_ids:
        await session.execute(
            delete(HireSelfIdentification).where(
                HireSelfIdentification.application_id.in_(scope.app_ids)
            )
        )
    return count


async def _erase_conversation(session: AsyncSession, scope: _Scope, now: datetime) -> int:
    count = 0
    for message in await session.scalars(
        select(HireMessage).where(HireMessage.id.in_(scope.message_ids)).with_for_update()
    ):
        message.variables = _sealed(
            HireMessage, "variables", {}, org_id=message.org_id, row_id=message.id
        )
        message.rendered = None
        message.updated_at = now
        if message.model_call_id is not None:
            scope.model_call_ids.add(message.model_call_id)
        count += 1
    return count


async def _erase_pipeline(session: AsyncSession, scope: _Scope) -> int:
    count = 0
    for note in await session.scalars(
        select(HireNote).where(HireNote.id.in_(scope.note_ids)).with_for_update()
    ):
        note.body = _sealed(HireNote, "body", ERASED_TEXT, org_id=note.org_id, row_id=note.id)
        count += 1
    if scope.app_ids:
        for move in await session.scalars(
            select(HireStageMove)
            .where(
                HireStageMove.application_id.in_(scope.app_ids), HireStageMove.reason.is_not(None)
            )
            .with_for_update()
        ):
            move.reason = None
    return count


async def _erase_interviews(session: AsyncSession, scope: _Scope, now: datetime) -> int:
    count = 0
    for interview in await session.scalars(
        select(HireInterview).where(HireInterview.id.in_(scope.interview_ids)).with_for_update()
    ):
        interview.candidate_timezone = ERASED_TIMEZONE
        interview.meeting_url = None
        interview.link_nonce = None
        interview.link_expires_at = None
        interview.updated_at = now
        count += 1
    return count


async def _erase_feedback(session: AsyncSession, scope: _Scope) -> int:
    """Scorecard notes and debrief texts about the candidate's applications."""
    if not scope.app_ids:
        return 0
    count = 0
    for card in await session.scalars(
        select(HireScorecard)
        .where(HireScorecard.application_id.in_(scope.app_ids), HireScorecard.notes != "")
        .with_for_update()
    ):
        # The freeze trigger lets a submitted scorecard's notes be cleared, nothing else.
        card.notes = ""
        count += 1
    for debrief in await session.scalars(
        select(HireDebrief).where(HireDebrief.application_id.in_(scope.app_ids)).with_for_update()
    ):
        if debrief.model_call_id is not None:
            scope.model_call_ids.add(debrief.model_call_id)
        debrief.content = {}
        if debrief.decision_reason is not None:
            debrief.decision_reason = ERASED_TEXT
        count += 1
    return count


async def _erase_references(session: AsyncSession, scope: _Scope, now: datetime) -> int:
    """Referees' names, addresses and answers, and the kept summaries."""
    count = 0
    for ref in await session.scalars(
        select(HireReferenceRequest)
        .where(HireReferenceRequest.id.in_(scope.reference_ids))
        .with_for_update()
    ):
        org, rid = ref.org_id, ref.id
        ref.referee_name = _sealed(
            HireReferenceRequest, "referee_name", ERASED_TEXT, org_id=org, row_id=rid
        )
        ref.referee_email = _sealed(
            HireReferenceRequest, "referee_email", erased_email(rid), org_id=org, row_id=rid
        )
        ref.answers = None
        ref.erased_at = ref.erased_at or now
        ref.updated_at = now
        count += 1
    if scope.reference_ids:
        for link in await session.scalars(
            select(HireReferenceLink)
            .where(
                HireReferenceLink.request_id.in_(scope.reference_ids),
                HireReferenceLink.used_at.is_(None),
            )
            .with_for_update()
        ):
            link.used_at = now
        for ref_session in await session.scalars(
            select(HireReferenceSession)
            .where(
                HireReferenceSession.request_id.in_(scope.reference_ids),
                HireReferenceSession.revoked_at.is_(None),
            )
            .with_for_update()
        ):
            ref_session.revoked_at = now
    if scope.app_ids:
        for summary in await session.scalars(
            select(HireReferenceSummary)
            .where(HireReferenceSummary.application_id.in_(scope.app_ids))
            .with_for_update()
        ):
            if summary.model_call_id is not None:
                scope.model_call_ids.add(summary.model_call_id)
            summary.content = _sealed(
                HireReferenceSummary,
                "content",
                dict(ERASED_MARK),
                org_id=summary.org_id,
                row_id=summary.id,
            )
    return count


async def _erase_offers(session: AsyncSession, scope: _Scope, now: datetime) -> int:
    count = 0
    for offer in await session.scalars(
        select(HireOffer).where(HireOffer.id.in_(scope.offer_ids)).with_for_update()
    ):
        offer.compensation = _sealed(
            HireOffer, "compensation", dict(ERASED_MARK), org_id=offer.org_id, row_id=offer.id
        )
        offer.response_note = None
        offer.erased_at = offer.erased_at or now
        offer.updated_at = now
        count += 1
    return count


def _public_scoring(raw: Any) -> dict[str, Any] | None:
    """The scores of a screening without anything read from the CV."""
    if not isinstance(raw, Mapping):
        return None
    criteria = []
    for c in raw.get("criteria", []):
        if not isinstance(c, Mapping):
            continue
        criteria.append(
            {
                k: c[k]
                for k in ("criterion_id", "name", "weight", "must_have", "model_score", "score")
                if k in c
            }
            | {"flags": [str(f) for f in c.get("flags", [])], "evidence": []}
        )
    kept = {
        k: raw[k] for k in ("overall", "shares", "rubric_version", "rubric_policy_id") if k in raw
    }
    return {**kept, "criteria": criteria}


async def _erase_screenings(session: AsyncSession, scope: _Scope, now: datetime) -> int:
    count = 0
    for row in await _screenings(session, scope):
        details = read_details(row)
        kept: dict[str, Any] = {"erased": True}
        scoring = _public_scoring(details.get("scoring"))
        if scoring is not None:
            kept["scoring"] = scoring
        override = details.get("override")
        if isinstance(override, Mapping):
            kept["override"] = {
                k: override[k] for k in ("scores", "overall", "decision_id") if k in override
            }
        row.details = _sealed(HireScreening, "details", kept, org_id=row.org_id, row_id=row.id)
        row.updated_at = now
        count += 1
    return count


async def _erase_portal(session: AsyncSession, scope: _Scope) -> None:
    cid = scope.candidate_id
    await session.execute(delete(HirePortalSession).where(HirePortalSession.candidate_id == cid))
    await session.execute(delete(HirePortalToken).where(HirePortalToken.candidate_id == cid))
    for row in await session.scalars(
        select(HireDataRequest)
        .where(HireDataRequest.candidate_id == cid, HireDataRequest.note.is_not(None))
        .with_for_update()
    ):
        row.note = None


# ------------------------------------------------------------------ core tables


async def _erase_documents(session: AsyncSession, scope: _Scope, now: datetime) -> int:
    count = 0
    inbound: set[uuid.UUID] = set()
    for doc in await session.scalars(
        select(Document).where(Document.id.in_(scope.document_ids)).with_for_update()
    ):
        doc.original_filename = None
        doc.parse_warnings = []
        doc.status = DocumentStatus.ERASED.value
        doc.status_reason = "erased"
        doc.needs_review = False
        doc.erased_at = doc.erased_at or now
        if doc.inbound_email_id is not None:
            inbound.add(doc.inbound_email_id)
        count += 1
    for mail in await session.scalars(
        select(InboundEmail).where(InboundEmail.id.in_(inbound)).with_for_update()
    ):
        mail.sender_address = None
        mail.sender_address_bidx = None
        mail.subject = None
        mail.forwarded_chain = []
    if scope.extraction_ids:
        await session.execute(
            text("SELECT erase_extraction_values(:ids)"), {"ids": list(scope.extraction_ids)}
        )
    return count


async def _erase_search(session: AsyncSession, scope: _Scope) -> int:
    removed = await remove_candidate(session, scope.candidate_id)
    sources = {scope.candidate_id, *scope.document_ids, *scope.app_ids}
    result = await session.execute(delete(SearchChunk).where(SearchChunk.source_id.in_(sources)))
    return removed + int(getattr(result, "rowcount", 0) or 0)


async def _erase_notifications(session: AsyncSession, scope: _Scope, now: datetime) -> int:
    ids = scope.entity_ids()
    conditions = [Notification.dedupe_key.contains(i) for i in ids]
    conditions.append(Notification.recipient_bidx.in_(scope.email_bidxs))
    rows = list(
        await session.scalars(
            select(Notification)
            .where(or_(*conditions), Notification.erased_at.is_(None))
            .with_for_update()
        )
    )
    # A digest that delivered one of them repeats its text.
    digests = {r.digest_id for r in rows if r.digest_id is not None} - {r.id for r in rows}
    if digests:
        rows += list(
            await session.scalars(
                select(Notification)
                .where(Notification.id.in_(digests), Notification.erased_at.is_(None))
                .with_for_update()
            )
        )
    for row in rows:
        row.content = None
        if row.recipient_user_id is None:
            row.recipient_address = _sealed(
                Notification,
                "recipient_address",
                erased_email(scope.candidate_id),
                org_id=row.org_id,
                row_id=row.id,
            )
            row.recipient_bidx = erased_bidx(scope.candidate_id)
        else:
            row.recipient_address = None
            row.recipient_bidx = None
        if row.status in {"queued", "digest"}:
            row.status = "suppressed"
            row.reason = "erased"
            row.next_attempt_at = None
        row.erased_at = now
    return len(rows)


async def _erase_workflow(session: AsyncSession, scope: _Scope, now: datetime) -> int:
    ids = scope.entity_ids()
    approvals = 0
    for approval in await session.scalars(
        select(Approval)
        .where(Approval.entity_id.in_(ids), Approval.erased_at.is_(None))
        .with_for_update()
    ):
        approval.context = dict(ERASED_MARK)
        approval.payload = dict(ERASED_MARK)
        approval.erased_at = now
        approvals += 1
    threads = list(
        await session.scalars(select(WorkflowRun.thread_id).where(WorkflowRun.entity_id.in_(ids)))
    )
    if threads:
        for statement in _CHECKPOINT_DELETES:
            await session.execute(statement, {"t": threads})
    return approvals


async def _erase_decisions(
    session: AsyncSession, scope: _Scope, actor_type: ActorType, actor_id: str
) -> int:
    ids = scope.entity_ids()
    rows = list(
        await session.scalars(
            select(DecisionRecord)
            .where(
                or_(
                    DecisionRecord.entity_id.in_(ids),
                    DecisionRecord.id.in_(scope.decision_ids),
                )
            )
            .with_for_update()
        )
    )
    for row in rows:
        scope.model_call_ids |= set(row.model_call_ids)
    erased = await erase_sensitive(
        session,
        rows,
        actor_type=actor_type,
        actor_id=actor_id,
        workflow=WORKFLOW,
        payload={"candidate_id": str(scope.candidate_id)},
    )
    for appeal in await session.scalars(
        select(Appeal)
        .where(Appeal.id.in_(scope.appeal_ids), Appeal.reason_erased_at.is_(None))
        .with_for_update()
    ):
        appeal.reason = _sealed(
            Appeal, "reason", ERASED_TEXT, org_id=appeal.org_id, row_id=appeal.id
        )
        appeal.reason_erased_at = datetime.now(UTC)
    return len(erased)


async def _erase_model_calls(session: AsyncSession, scope: _Scope, now: datetime) -> int:
    for extraction in await session.scalars(
        select(Extraction).where(Extraction.id.in_(scope.extraction_ids))
    ):
        scope.model_call_ids |= set(extraction.model_call_ids)
    entity_ids = scope.entity_ids()
    first = list(
        await session.scalars(
            select(ModelCall).where(
                or_(ModelCall.id.in_(scope.model_call_ids), ModelCall.entity_id.in_(entity_ids))
            )
        )
    )
    if not first:
        return 0
    chains = {c.chain_id for c in first}
    ids = {c.id for c in first}
    # The recording a call was replayed from, and any call with the very same request,
    # hold the same personal data.
    sources = {c.source_call_id for c in first if c.source_call_id is not None}
    hashes = {c.request_hash for c in first}
    rows = list(
        await session.scalars(
            select(ModelCall)
            .where(
                or_(
                    ModelCall.id.in_(ids | sources),
                    ModelCall.chain_id.in_(chains),
                    ModelCall.replay_of.in_(ids),
                    ModelCall.source_call_id.in_(ids),
                    ModelCall.request_hash.in_(hashes),
                ),
                ModelCall.erased_at.is_(None),
            )
            .with_for_update()
        )
    )
    for call in rows:
        call.request = _sealed(
            ModelCall, "request", dict(ERASED_MARK), org_id=call.org_id, row_id=call.id
        )
        call.response = None
        call.erased_at = now
    return len(rows)


# ------------------------------------------------------------------ the request


async def _open_request(session: AsyncSession, request_id: uuid.UUID) -> HireDataRequest:
    row = await session.scalar(
        select(HireDataRequest).where(HireDataRequest.id == request_id).with_for_update()
    )
    if row is None:
        raise NotFoundError("request not found")
    if row.kind != DataRequestKind.DELETION.value:
        raise ConflictError("only a deletion request can erase data")
    if row.status != DataRequestStatus.OPEN.value:
        raise ConflictError("this request was already resolved")
    return row


async def erase_candidate(
    session: AsyncSession,
    candidate: HireCandidate,
    *,
    actor_type: ActorType,
    actor_id: str,
) -> ErasureCounts:
    """Clear every row about ``candidate`` (step 1), in the caller's transaction.
    Idempotent: running it again clears what is new and changes nothing else."""
    now = datetime.now(UTC)
    scope = await _scope(session, candidate)
    await _erase_candidate(session, candidate, now)
    applications = await _erase_applications(session, scope, now)
    messages = await _erase_conversation(session, scope, now)
    notes = await _erase_pipeline(session, scope)
    interviews = await _erase_interviews(session, scope, now)
    feedback = await _erase_feedback(session, scope)
    references = await _erase_references(session, scope, now)
    offers = await _erase_offers(session, scope, now)
    screenings = await _erase_screenings(session, scope, now)
    await _erase_portal(session, scope)
    documents = await _erase_documents(session, scope, now)
    chunks = await _erase_search(session, scope)
    notifications = await _erase_notifications(session, scope, now)
    approvals = await _erase_workflow(session, scope, now)
    decisions = await _erase_decisions(session, scope, actor_type, actor_id)
    calls = await _erase_model_calls(session, scope, now)
    await session.flush()
    return ErasureCounts(
        applications=applications,
        documents=documents,
        documents_shared=len(scope.shared_document_ids),
        messages=messages,
        notes=notes,
        interviews=interviews,
        feedback=feedback,
        references=references,
        offers=offers,
        screenings=screenings,
        notifications=notifications,
        decisions=decisions,
        model_calls=calls,
        approvals=approvals,
        search_chunks=chunks,
    )


async def erase_for_request(
    session: AsyncSession, actor: Actor, request_id: uuid.UUID, *, reason: str
) -> tuple[HireDataRequest, HireCandidate | None, ErasureCounts]:
    """Step 1 for an open deletion request: erase the candidate's rows and mark the
    request as erasing (it stays open until ``finish_erasure`` removed the files).
    Returns the request, the candidate row and what was cleared."""
    row = await _open_request(session, request_id)
    candidate = await session.scalar(
        select(HireCandidate).where(HireCandidate.id == row.candidate_id).with_for_update()
    )
    counts = ErasureCounts()
    if candidate is not None:
        counts = await erase_candidate(
            session, candidate, actor_type=ActorType.USER, actor_id=str(actor.id)
        )
    now = datetime.now(UTC)
    row.erasure_started_at = row.erasure_started_at or now
    row.erasure_error = None
    row.handled_by = actor.id
    row.outcome = _sealed(HireDataRequest, "outcome", reason, org_id=row.org_id, row_id=row.id)
    await session.flush()
    await audit_actor_action(
        session,
        actor,
        ERASURE_STARTED,
        entity_type=ENTITY,
        entity_id=row.id,
        workflow=WORKFLOW,
        # The reason is sealed on the request (it may name the person); the audit keeps
        # who, when and what was cleared.
        payload={"candidate_id": str(row.candidate_id), **counts.as_payload()},
    )
    if candidate is not None:
        await session.refresh(candidate)
    await session.refresh(row)
    return row, candidate, counts


async def _pending_documents(session: AsyncSession, candidate_id: uuid.UUID) -> list[Document]:
    """The candidate's erased documents whose stored objects may still exist."""
    scope = _Scope(org_id=uuid.UUID(int=0), candidate_id=candidate_id)
    await _documents_of(session, scope)
    if not scope.document_ids:
        return []
    return list(
        await session.scalars(
            select(Document)
            .where(
                Document.id.in_(scope.document_ids),
                Document.status == DocumentStatus.ERASED.value,
                Document.objects_erased_at.is_(None),
            )
            .with_for_update()
        )
    )


async def _keys_to_delete(session: AsyncSession, docs: Iterable[Document]) -> list[str]:
    """Object keys of ``docs``, except those another live document still uses (the
    original bytes are stored once per organisation and hash, across workflows)."""
    keys: set[str] = set()
    for doc in docs:
        others = await session.scalar(
            select(Document.id)
            .where(
                Document.sha256 == doc.sha256,
                Document.id != doc.id,
                Document.status != DocumentStatus.ERASED.value,
            )
            .limit(1)
        )
        if others is not None:
            continue
        keys.add(blob_key(doc.org_id, doc.sha256))
        if doc.storage_key:
            keys.add(doc.storage_key)
        if doc.parse_result_key:
            keys.add(doc.parse_result_key)
    return sorted(keys)


async def finish_erasure(
    session: AsyncSession,
    store: ObjectStore | None,
    request_id: uuid.UUID,
    *,
    actor_type: ActorType,
    actor_id: str,
) -> HireDataRequest:
    """Step 2, after step 1 committed: delete the candidate's stored files, then complete
    the request. On a store failure the request stays open with ``erasure_error`` (the
    caller commits that), and the scheduled job tries again."""
    row = await session.scalar(
        select(HireDataRequest).where(HireDataRequest.id == request_id).with_for_update()
    )
    if row is None:
        raise NotFoundError("request not found")
    if row.status != DataRequestStatus.OPEN.value or row.erasure_started_at is None:
        raise ConflictError("this request has no erasure in progress")
    docs = await _pending_documents(session, row.candidate_id)
    error: str | None = None
    if docs and store is None:
        error = STORE_UNAVAILABLE
    elif docs and store is not None:
        try:
            for key in await _keys_to_delete(session, docs):
                await store.delete(key)
        except (StorageError, ValueError):
            error = STORE_FAILED
    now = datetime.now(UTC)
    row.erasure_attempts += 1
    if error is not None:
        row.erasure_error = error
        await session.flush()
        await append_audit(
            session,
            action=ERASURE_FAILED,
            actor_type=actor_type,
            actor_id=actor_id,
            entity_type=ENTITY,
            entity_id=row.id,
            workflow=WORKFLOW,
            payload={"error": error, "attempts": row.erasure_attempts},
        )
        await session.refresh(row)
        return row
    for doc in docs:
        doc.objects_erased_at = now
        doc.parse_result_key = None
    row.erasure_error = None
    row.status = DataRequestStatus.COMPLETED.value
    row.handled_at = now
    await session.flush()
    await append_audit(
        session,
        action=ERASED,
        actor_type=actor_type,
        actor_id=actor_id,
        entity_type=ENTITY,
        entity_id=row.id,
        workflow=WORKFLOW,
        payload={
            "candidate_id": str(row.candidate_id),
            "documents_removed": len(docs),
            "attempts": row.erasure_attempts,
            "reason_given": row.outcome is not None,
        },
    )
    await session.refresh(row)
    return row


async def pending_requests(session: AsyncSession) -> list[uuid.UUID]:
    """Open deletion requests of the session's organisation whose files remain."""
    return list(
        await session.scalars(
            select(HireDataRequest.id)
            .where(
                HireDataRequest.status == DataRequestStatus.OPEN.value,
                HireDataRequest.erasure_started_at.is_not(None),
            )
            .order_by(HireDataRequest.erasure_started_at)
        )
    )


# ------------------------------------------------------------------ retry job

JOB_NAME: Final = "hire_erasure_objects"


async def run_erasure_job(ctx: JobContext) -> dict[str, Any]:
    """``hire_erasure_objects``: every 5 minutes, finish the erasures a person started
    whose stored files could not be removed yet. It never starts an erasure."""
    store = ctx.runtime.services.store
    totals: dict[str, Any] = {"orgs": 0, "completed": 0, "failed": 0}
    async with ctx.sessionmaker() as scan, scan.begin():
        rows = await scan.execute(
            text("SELECT org_id FROM hire_erasure_pending_org_ids() AS org_id")
        )
        org_ids = [uuid.UUID(str(v)) for v in rows.scalars()]
    for org_id in org_ids:
        totals["orgs"] += 1
        session = ctx.sessionmaker()
        set_tenant(session, org_id, None)
        async with session:
            async with session.begin():
                request_ids = await pending_requests(session)
            for request_id in request_ids:
                try:
                    async with session.begin():
                        row = await finish_erasure(
                            session,
                            store,
                            request_id,
                            actor_type=ActorType.SYSTEM,
                            actor_id=JOB_ACTOR,
                        )
                        done = row.status == DataRequestStatus.COMPLETED.value
                except Exception as exc:
                    log.warning("hire_erasure_failed", org_id=str(org_id), error=type(exc).__name__)
                    totals["failed"] += 1
                    continue
                totals["completed" if done else "failed"] += 1
    return totals
