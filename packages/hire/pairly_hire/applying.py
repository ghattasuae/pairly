# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""The public apply flow (brief 3.2), in three steps, all in the job's organisation:

1. ``start_application``: the candidate accepted the AI-use notice and filled the form.
   Checks: the job is open, the notice version is current, bot protection, the email is
   real (not a disposable domain), the per-email limit (counted in the database), the
   answers fit the form and the locked rubric. Stores the candidate (personal fields
   encrypted, blind indexes for email, phone and name words), the application
   (``incomplete``, consent version, time and language), and the voluntary
   self-identification answers in their own table. Returns a one-time upload token.
2. ``attach_file``: the browser uploads the CV (required) and any optional files with
   the token, through the engine's ingestion (hashing, type sniffing, size limits,
   deduplication, virus scan and sandboxed parsing by the worker; workflow ``hire``).
3. ``submit_application``: with the CV present, the application becomes ``received``,
   duplicate hints are computed, knockout mismatches and duplicates set
   ``needs_human_review`` with reasons, the token is cleared and the acknowledgement
   email is queued (``hire.application.received``, the candidate's language).

Nothing here ever rejects anyone. Public actions are audited as the system actor
``hire.careers`` (there is no signed-in person); no personal data goes into audit
payloads.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from pairly_core.audit import ActorType, append_audit
from pairly_core.crypto.blind_index import blind_index, normalize_email
from pairly_core.crypto.types import seal, unseal
from pairly_core.errors import BadRequestError, ConflictError, NotFoundError, PairlyError
from pairly_core.ingestion.models import Document
from pairly_core.notifications.service import Recipient, notify
from pairly_core.policy_versions import get_policy
from pairly_hire.antispam import RateLimitedError, TurnstileVerifier, is_disposable
from pairly_hire.dedupe import find_duplicates, name_bidx
from pairly_hire.forms import (
    CV_FIELD,
    ApplicationForm,
    FieldMode,
    check_answers,
    clean_text,
    parse_form,
)
from pairly_hire.jobs import current_form
from pairly_hire.models import (
    ApplicationStatus,
    HireApplication,
    HireCandidate,
    HireJob,
    JobStatus,
)
from pairly_hire.notice import CURRENT_VERSION
from pairly_hire.rubrics import locked_rubric
from pairly_hire.selfid.models import HireSelfIdentification
from pairly_hire.selfid.schema import clean_answers
from pairly_hire.templates import RECEIVED

WORKFLOW: Final = "hire"
SYSTEM_ACTOR: Final = "hire.careers"
ENTITY: Final = "hire.application"
STARTED: Final = "hire.application.started"
FILE_ADDED: Final = "hire.application.file_added"
SUBMITTED: Final = "hire.application.received"
TOKEN_TTL: Final = timedelta(hours=2)
# At most this many applications per email address per organisation per day.
EMAIL_LIMIT_PER_DAY: Final = 5
_REF_ALPHABET: Final = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


class ApplyRefusedError(PairlyError):
    """The application cannot be accepted as sent. ``code`` says why (safe to show)."""

    status = 400
    title = "Application not accepted"

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code  # type: ignore[misc]


@dataclass(frozen=True, slots=True)
class Contact:
    full_name: str
    email: str
    phone: str | None
    location: str | None
    profile_url: str | None
    locale: str


@dataclass(frozen=True, slots=True)
class ApplyRequest:
    contact: Contact
    questions: dict[str, Any]
    knockouts: dict[str, Any]
    self_identification: dict[str, Any]
    notice_version: str
    consent: bool
    turnstile_token: str


@dataclass(frozen=True, slots=True)
class Started:
    application: HireApplication
    token: str
    expires_at: datetime
    files: frozenset[str]


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_reference() -> str:
    raw = "".join(secrets.choice(_REF_ALPHABET) for _ in range(8))
    return f"{raw[:4]}-{raw[4:]}"


def _audit(session: AsyncSession, action: str, app_id: uuid.UUID, payload: dict[str, Any]) -> Any:
    return append_audit(
        session,
        action=action,
        actor_type=ActorType.SYSTEM,
        actor_id=SYSTEM_ACTOR,
        entity_type=ENTITY,
        entity_id=app_id,
        workflow=WORKFLOW,
        payload=payload,
    )


def _contact_value(form: ApplicationForm, name: str, value: str | None, limit: int) -> str | None:
    mode = getattr(form.standard, name)
    text = clean_text(value or "", limit) if value else ""
    if mode == FieldMode.HIDDEN:
        return None
    if mode == FieldMode.REQUIRED and not text:
        raise BadRequestError(f"{name.replace('_', ' ')} is required")
    return text or None


def _check_profile_url(value: str | None) -> str | None:
    if value is None:
        return None
    if not value.startswith("https://") or len(value) > 300 or any(c.isspace() for c in value):
        raise BadRequestError("the profile link must be an https:// address")
    return value


def _differs(candidate: HireCandidate, personal: dict[str, str | None]) -> bool:
    """Whether the name or phone sent now differs from what the candidate row holds."""
    for column in ("full_name", "phone"):
        sent = personal.get(column)
        if sent is None or getattr(candidate, column) is None:
            continue
        if str(unseal(candidate, column)).strip().casefold() != sent.strip().casefold():
            return True
    return False


def _seal_into(candidate: HireCandidate, column: str, value: str, org_id: uuid.UUID) -> None:
    """Write an encrypted personal field (bound to this organisation, table and row)."""
    setattr(
        candidate, column, seal(HireCandidate, column, value, org_id=org_id, row_id=candidate.id)
    )


async def _emails_today(session: AsyncSession, email_bidx: str) -> int:
    since = datetime.now(UTC) - timedelta(days=1)
    count = await session.scalar(
        select(func.count())
        .select_from(HireApplication)
        .join(HireCandidate, HireCandidate.id == HireApplication.candidate_id)
        .where(HireCandidate.email_bidx == email_bidx, HireApplication.created_at > since)
    )
    return int(count or 0)


async def start_application(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    job: HireJob,
    request: ApplyRequest,
    verifier: TurnstileVerifier,
    remote_ip: str | None,
) -> Started:
    if job.status != JobStatus.OPEN.value:
        raise NotFoundError("job not found")
    if not request.consent or request.notice_version != CURRENT_VERSION:
        raise ApplyRefusedError(
            "consent_required", "please read and accept the current notice on how AI is used"
        )
    if not await verifier.verify(request.turnstile_token, remote_ip):
        raise ApplyRefusedError("bot_check_failed", "the human check failed, please try again")

    contact = request.contact
    email = normalize_email(contact.email or "")
    if email is None or len(email) > 254:
        raise ApplyRefusedError("invalid_email", "please give a valid email address")
    if is_disposable(email):
        raise ApplyRefusedError(
            "disposable_email", "please use an email address you will keep, not a temporary one"
        )
    email_bidx = blind_index("email", email, org_id)
    if email_bidx is None:  # pragma: no cover - normalize_email already passed
        raise ApplyRefusedError("invalid_email", "please give a valid email address")
    if await _emails_today(session, email_bidx) >= EMAIL_LIMIT_PER_DAY:
        raise RateLimitedError(3600, "too many applications from this email address today")

    full_name = clean_text(contact.full_name or "", 160)
    if len(full_name) < 2:
        raise BadRequestError("full name is required")
    form, _ = await current_form(session, job)
    rubric_policy, rubric = await locked_rubric(session, job)
    if job.form_policy_id is None:  # pragma: no cover - an open job always has both
        raise ConflictError("the job has no application form")
    phone = _contact_value(form, "phone", contact.phone, 40)
    location = _contact_value(form, "location", contact.location, 160)
    profile_url = _check_profile_url(_contact_value(form, "profile_url", contact.profile_url, 300))
    answers = check_answers(form, rubric, request.questions, request.knockouts)
    self_id = clean_answers(form.self_identification.active, request.self_identification)
    locale = contact.locale if contact.locale in ("en", "ar") else "en"

    candidate = await session.scalar(
        select(HireCandidate).where(HireCandidate.email_bidx == email_bidx).with_for_update()
    )
    now = datetime.now(UTC)
    if candidate is None:
        candidate = HireCandidate(id=uuid.uuid4(), org_id=org_id, email_bidx=email_bidx)
        session.add(candidate)
    cid = candidate.id
    personal: dict[str, str | None] = {
        "email": email,
        "full_name": full_name,
        "phone": phone,
        "location": location,
        "profile_url": profile_url,
    }
    # The candidate row is written once per address and only ever filled in, never
    # overwritten from this public form: the application keeps its own copy (``contact``).
    review = list(answers.review_reasons)
    is_new = getattr(candidate, "full_name", None) is None
    if not is_new and _differs(candidate, personal):
        review.append("contact_differs")
    for column, value in personal.items():
        if value is not None and getattr(candidate, column, None) is None:
            _seal_into(candidate, column, value, org_id)
    if is_new:
        candidate.name_bidx = name_bidx(full_name, org_id)
        candidate.locale = locale
    if phone is not None and candidate.phone_bidx is None:
        candidate.phone_bidx = blind_index("phone", phone, org_id)
    candidate.updated_at = now
    try:
        await session.flush()
    except IntegrityError:  # two first applications with one address at the same moment
        raise ConflictError("please try again") from None

    token = secrets.token_urlsafe(32)
    app_id = uuid.uuid4()
    app = HireApplication(
        id=app_id,
        org_id=org_id,
        job_id=job.id,
        candidate_id=cid,
        reference=new_reference(),
        status=ApplicationStatus.INCOMPLETE.value,
        rubric_policy_id=rubric_policy.id,
        form_policy_id=job.form_policy_id,
        answers=seal(HireApplication, "answers", answers.stored(), org_id=org_id, row_id=app_id),
        contact=seal(
            HireApplication,
            "contact",
            {**personal, "locale": locale},
            org_id=org_id,
            row_id=app_id,
        ),
        review_reasons=review,
        needs_human_review=bool(review),
        duplicate_signals=[],
        consent_version=request.notice_version,
        consent_at=now,
        consent_locale=locale,
        files={},
        upload_token_hash=hash_token(token),
        upload_token_expires_at=now + TOKEN_TTL,
    )
    session.add(app)
    try:
        await session.flush()
    except IntegrityError:  # pragma: no cover - reference collision, astronomically rare
        raise ConflictError("please try again") from None
    if self_id:
        row_id = uuid.uuid4()
        session.add(
            HireSelfIdentification(
                id=row_id,
                org_id=org_id,
                application_id=app_id,
                job_id=job.id,
                answers=seal(
                    HireSelfIdentification, "answers", self_id, org_id=org_id, row_id=row_id
                ),
            )
        )
        await session.flush()
    await _audit(
        session,
        STARTED,
        app_id,
        {
            "job_id": str(job.id),
            "consent_version": request.notice_version,
            "knockout_mismatches": len(answers.review_reasons),
            "self_identification": bool(self_id),
        },
    )
    await session.refresh(app)
    return Started(app, token, now + TOKEN_TTL, form.file_ids)


async def application_for_token(session: AsyncSession, token: str) -> HireApplication:
    """The incomplete application a live upload token belongs to (locked), or 404."""
    if not token or len(token) > 100:
        raise NotFoundError("application not found or the link expired")
    app = await session.scalar(
        select(HireApplication)
        .where(HireApplication.upload_token_hash == hash_token(token))
        .with_for_update()
    )
    now = datetime.now(UTC)
    if (
        app is None
        or app.status != ApplicationStatus.INCOMPLETE.value
        or app.upload_token_expires_at is None
        or app.upload_token_expires_at <= now
    ):
        raise NotFoundError("application not found or the link expired")
    return app


async def file_fields(session: AsyncSession, app: HireApplication) -> frozenset[str]:
    """The file fields of the form version the application answers."""
    policy = await get_policy(session, app.form_policy_id)
    if policy is None:  # pragma: no cover - RESTRICT foreign key
        raise NotFoundError("application not found")
    return parse_form(policy.content).file_ids


async def attach_file(
    session: AsyncSession, app: HireApplication, field: str, document: Document, *, created: bool
) -> None:
    if field == CV_FIELD:
        app.cv_document_id = document.id
    else:
        app.files = {**dict(app.files or {}), field: str(document.id)}
    app.updated_at = datetime.now(UTC)
    await session.flush()
    await _audit(
        session,
        FILE_ADDED,
        app.id,
        {"field": field, "document_id": str(document.id), "file_seen_before": not created},
    )


@dataclass(frozen=True, slots=True)
class Submitted:
    application: HireApplication
    duplicates: int


async def submit_application(
    session: AsyncSession,
    app: HireApplication,
    *,
    org_name: str,
    job_title: str,
    response_days: int,
    portal_url: str | None,
) -> Submitted:
    if app.cv_document_id is None:
        raise ApplyRefusedError("cv_required", "please upload your CV before you submit")
    candidate = await session.scalar(
        select(HireCandidate).where(HireCandidate.id == app.candidate_id)
    )
    if candidate is None:  # pragma: no cover - RESTRICT foreign key
        raise NotFoundError("application not found")
    now = datetime.now(UTC)
    duplicates = await find_duplicates(session, app, candidate)
    reasons = list(app.review_reasons or [])
    if duplicates and "possible_duplicate" not in reasons:
        reasons.append("possible_duplicate")
    app.duplicate_signals = duplicates
    app.review_reasons = reasons
    app.needs_human_review = bool(reasons)
    app.status = ApplicationStatus.RECEIVED.value
    app.submitted_at = now
    app.upload_token_hash = None
    app.upload_token_expires_at = None
    app.updated_at = now
    await session.flush()
    await _audit(
        session,
        SUBMITTED,
        app.id,
        {
            "job_id": str(app.job_id),
            "duplicate_hints": len(duplicates),
            "needs_human_review": app.needs_human_review,
            "files": sorted(app.files or {}),
        },
    )
    email = str(unseal(candidate, "email"))
    contact = unseal(app, "contact") or {}
    name = contact.get("full_name") if isinstance(contact, dict) else None
    results = await notify(
        session,
        recipient=Recipient.external(email, locale=app.consent_locale),
        template_key=RECEIVED,
        variables={
            "candidate_name": str(name or unseal(candidate, "full_name")),
            "job_title": job_title,
            "org_name": org_name,
            "reference": app.reference,
            "response_days": response_days,
            "portal_url": portal_url,
        },
        dedupe_key=f"hire.ack:{app.id}",
        channels=["email"],
        workflow=WORKFLOW,
        actor_type=ActorType.SYSTEM,
        actor_id=SYSTEM_ACTOR,
    )
    if any(r.notification_id is not None for r in results):
        app.acknowledged_at = now
        await session.flush()
    await session.refresh(app)
    return Submitted(app, len(duplicates))
