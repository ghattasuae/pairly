# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""The candidate experience survey (brief 3.8.5).

Invites (``send_due_invites``, the ``hire_survey_invites`` job, every 15 minutes): one
per application once its process ended, for every outcome: hired, withdrawn, and
rejected once the rejection was sent (never before the candidate knows). ``DELAY``
after the move, so an undone move invites nobody. A candidate who turned update emails
off gets no email (the invite is recorded as ``skipped``).

The link::

    sv1.<org hex>.<invite id hex>.<secret>

``secret`` is 32 random bytes (URL-safe base64); the invite stores only
``sha256("pairly.hire.survey|<org>|<invite>|<secret>")``. Single use, ``TTL`` (30 days).
Reading the form (``open_form``) does not use it; answering (``answer``) does.

Anonymous by default: the response row has no invite, candidate or application id and
only the day it was answered; the invite keeps only the day it was used. The candidate
may attach their name (``attach_name``): then the response names the candidate and the
application. ``aggregate_results`` gives counts and averages only, and hides any group
smaller than ``MIN_GROUP``; ``named_responses`` lists only the responses whose authors
chose to be named.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from statistics import mean
from typing import Any, Final

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from pairly_core.audit import ActorType, append_audit
from pairly_core.crypto.types import seal
from pairly_core.db import set_tenant
from pairly_core.errors import NotFoundError
from pairly_core.jobs.registry import JobContext
from pairly_core.notifications.service import Recipient, notify
from pairly_hire.links import LinkConfig, links_from_env
from pairly_hire.messaging.models import HireMessage, MessagePurpose, MessageStatus
from pairly_hire.messaging.prefs import prefs_for
from pairly_hire.models import HireApplication, HireCandidate, HireJob
from pairly_hire.pipeline.models import HirePipelineEntry
from pairly_hire.pipeline.records import load_record, organisation_name
from pairly_hire.pipeline.stages import HIRED, REJECTED, WITHDRAWN
from pairly_hire.sealing import read_sealed
from pairly_hire.survey import form
from pairly_hire.survey.models import HireSurveyInvite, HireSurveyResponse
from pairly_hire.survey.templates import SURVEY_INVITE

log = structlog.get_logger("pairly_hire.survey")

PREFIX: Final = "sv1"
TTL: Final = timedelta(days=30)
DELAY: Final = timedelta(hours=2)
MAX_PER_RUN: Final = 200
MAX_TOKEN: Final = 200
MIN_GROUP: Final = 5
WORKFLOW: Final = "hire"
ENTITY: Final = "hire.survey_invite"
JOB_NAME: Final = "hire_survey_invites"
JOB_ACTOR: Final = "pairly.hire.survey"
INVALID: Final = "this link is not valid any more"
_OUTCOMES: Final = {HIRED: "hired", REJECTED: "rejected", WITHDRAWN: "withdrawn"}


def _digest(org: uuid.UUID, invite: uuid.UUID, secret: str) -> str:
    raw = f"pairly.hire.survey|{org.hex}|{invite.hex}|{secret}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _secret() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii").rstrip("=")


def _uuid(value: str) -> uuid.UUID | None:
    if len(value) != 32:
        return None
    try:
        return uuid.UUID(hex=value)
    except ValueError:
        return None


@dataclass(frozen=True, slots=True)
class Claim:
    org_id: uuid.UUID
    invite_id: uuid.UUID
    secret: str


def parse(token: str) -> Claim | None:
    if not isinstance(token, str) or len(token) > MAX_TOKEN:
        return None
    parts = token.split(".")
    if len(parts) != 4 or parts[0] != PREFIX:
        return None
    org, invite = _uuid(parts[1]), _uuid(parts[2])
    secret = parts[3]
    if org is None or invite is None or len(secret) != 43:
        return None
    if not all(c.isascii() and (c.isalnum() or c in "-_") for c in secret):
        return None
    return Claim(org, invite, secret)


def survey_url(links: LinkConfig, locale: str, token: str) -> str | None:
    return f"{links.web_url}/{locale}/survey#{token}" if links.web_url else None


# ------------------------------------------------------------------ invites


async def _rejection_sent(session: AsyncSession, application_id: uuid.UUID) -> bool:
    found = await session.scalar(
        select(HireMessage.id).where(
            HireMessage.application_id == application_id,
            HireMessage.purpose == MessagePurpose.REJECTION.value,
            HireMessage.status.in_([MessageStatus.QUEUED.value, MessageStatus.SENT.value]),
        )
    )
    return found is not None


async def due_applications(session: AsyncSession, now: datetime) -> list[tuple[uuid.UUID, str]]:
    """Finished applications without an invite yet: (application id, outcome)."""
    rows = (
        await session.execute(
            select(HireApplication.id, HirePipelineEntry.stage_key)
            .join(HirePipelineEntry, HirePipelineEntry.application_id == HireApplication.id)
            .join(HireCandidate, HireCandidate.id == HireApplication.candidate_id)
            .outerjoin(HireSurveyInvite, HireSurveyInvite.application_id == HireApplication.id)
            .where(
                HirePipelineEntry.stage_key.in_(list(_OUTCOMES)),
                HirePipelineEntry.stage_entered_at <= now - DELAY,
                HireSurveyInvite.id.is_(None),
                HireCandidate.erased_at.is_(None),
            )
            .order_by(HirePipelineEntry.stage_entered_at)
            .limit(MAX_PER_RUN)
        )
    ).all()
    out: list[tuple[uuid.UUID, str]] = []
    for app_id, stage in rows:
        if stage == REJECTED and not await _rejection_sent(session, app_id):
            continue
        out.append((app_id, _OUTCOMES[stage]))
    return out


async def invite(
    session: AsyncSession,
    application_id: uuid.UUID,
    outcome: str,
    *,
    links: LinkConfig,
    now: datetime | None = None,
) -> HireSurveyInvite:
    """Record the invite and email the candidate their single use link."""
    now = now or datetime.now(UTC)
    record = await load_record(session, application_id)
    locale = record.locale
    wants = (await prefs_for(session, record.candidate.id)).updates_email
    secret = _secret()
    row = HireSurveyInvite(
        id=uuid.uuid4(),
        org_id=record.app.org_id,
        application_id=record.app.id,
        candidate_id=record.candidate.id,
        job_id=record.job.id,
        outcome=outcome,
        locale=locale,
        status="sent" if wants else "skipped",
        secret_hash="0" * 64,
        expires_at=now + TTL,
        created_at=now,
    )
    row.secret_hash = _digest(row.org_id, row.id, secret)
    session.add(row)
    await session.flush()
    token = f"{PREFIX}.{row.org_id.hex}.{row.id.hex}.{secret}"
    url = survey_url(links, locale, token)
    if wants and url is not None:
        title = record.job.title_ar if locale == "ar" and record.job.title_ar else record.job.title
        await notify(
            session,
            recipient=Recipient.external(
                str(read_sealed(record.candidate, "email")), locale=locale
            ),
            template_key=SURVEY_INVITE,
            variables={
                "candidate_name": record.full_name,
                "job_title": title,
                "company": await organisation_name(session, row.org_id),
                "survey_url": url,
            },
            dedupe_key=f"hire.survey:{row.id}",
            workflow=WORKFLOW,
            actor_type=ActorType.SYSTEM,
            actor_id=JOB_ACTOR,
        )
    elif url is None:
        row.status = "skipped"
    await append_audit(
        session,
        action="hire.survey.invited" if row.status == "sent" else "hire.survey.skipped",
        actor_type=ActorType.SYSTEM,
        actor_id=JOB_ACTOR,
        entity_type=ENTITY,
        entity_id=row.id,
        workflow=WORKFLOW,
        payload={"application_id": str(row.application_id), "outcome": outcome},
    )
    return row


async def send_due_invites(
    session: AsyncSession, links: LinkConfig, now: datetime | None = None
) -> int:
    now = now or datetime.now(UTC)
    count = 0
    for app_id, outcome in await due_applications(session, now):
        await invite(session, app_id, outcome, links=links, now=now)
        count += 1
    return count


async def run_invites_job(ctx: JobContext) -> dict[str, Any]:
    """``hire_survey_invites``: every 15 minutes, per organisation with applicants."""
    from pairly_hire.pipeline.rediscovery import org_ids

    links = links_from_env()
    totals: dict[str, Any] = {"orgs": 0, "invited": 0, "failed": 0}
    for org_id in await org_ids(ctx):
        session = ctx.sessionmaker()
        set_tenant(session, org_id, None)
        try:
            async with session, session.begin():
                totals["invited"] += await send_due_invites(session, links)
        except Exception as exc:
            log.warning("survey_invites_failed", org_id=str(org_id), error=type(exc).__name__)
            totals["failed"] += 1
            continue
        totals["orgs"] += 1
    return totals


# ------------------------------------------------------------------ answering


async def _valid_invite(
    session: AsyncSession, token: str, *, lock: bool, now: datetime
) -> HireSurveyInvite:
    claim = parse(token)
    if claim is None:
        raise NotFoundError(INVALID)
    set_tenant(session, claim.org_id, None)
    query = select(HireSurveyInvite).where(HireSurveyInvite.id == claim.invite_id)
    if lock:
        query = query.with_for_update()
    row = await session.scalar(query)
    if (
        row is None
        or not hmac.compare_digest(
            _digest(claim.org_id, claim.invite_id, claim.secret), row.secret_hash
        )
        or row.status != "sent"
        or row.expires_at <= now
    ):
        raise NotFoundError(INVALID)
    return row


@dataclass(frozen=True, slots=True)
class SurveyForm:
    company: str
    job_title: str
    questions: list[dict[str, Any]]


async def open_form(
    session: AsyncSession, token: str, locale: str, *, now: datetime | None = None
) -> SurveyForm:
    """The form for a valid link (the database session must be fresh). Does not use it."""
    row = await _valid_invite(session, token, lock=False, now=now or datetime.now(UTC))
    job = await session.scalar(select(HireJob).where(HireJob.id == row.job_id))
    lang = "ar" if locale == "ar" else "en"
    title = ""
    if job is not None:
        title = job.title_ar if lang == "ar" and job.title_ar else job.title
    return SurveyForm(
        company=await organisation_name(session, row.org_id),
        job_title=title,
        questions=form.questions_out(lang),
    )


async def answer(
    session: AsyncSession,
    token: str,
    raw: dict[str, Any],
    *,
    attach_name: bool,
    now: datetime | None = None,
) -> None:
    """Use the link once and store the response, anonymous unless ``attach_name``."""
    now = now or datetime.now(UTC)
    row = await _valid_invite(session, token, lock=True, now=now)
    answers, comment = form.check_answers(raw)
    today = now.date()
    row.status = "used"
    row.used_on = today
    response = HireSurveyResponse(
        id=uuid.uuid4(),
        org_id=row.org_id,
        job_id=row.job_id,
        outcome=row.outcome,
        locale=row.locale,
        answers=answers,
        named=attach_name,
        candidate_id=row.candidate_id if attach_name else None,
        application_id=row.application_id if attach_name else None,
        answered_on=today,
    )
    if comment:
        response.comment = seal(  # type: ignore[assignment]
            HireSurveyResponse, "comment", comment, org_id=row.org_id, row_id=response.id
        )
    session.add(response)
    await session.flush()
    # One event, on the invite: who answered stays unknown unless they chose otherwise.
    await append_audit(
        session,
        action="hire.survey.answered",
        actor_type=ActorType.CANDIDATE,
        actor_id=f"hire_candidate:{row.candidate_id}" if attach_name else "hire_survey:anonymous",
        entity_type=ENTITY,
        entity_id=row.id,
        workflow=WORKFLOW,
        payload={"named": attach_name, "form": form.FORM_VERSION},
    )


# ------------------------------------------------------------------ results


@dataclass(frozen=True, slots=True)
class GroupResult:
    """Counts and averages for one group; ``None`` where the group is too small."""

    responses: int
    suppressed: bool
    averages: dict[str, float | None] = field(default_factory=dict)
    recommend: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SurveyResults:
    invited: int
    responses: int
    response_rate: float | None
    overall: GroupResult
    by_outcome: dict[str, GroupResult]
    min_group: int


def _group(rows: list[HireSurveyResponse], *, min_group: int) -> GroupResult:
    if len(rows) < min_group:
        return GroupResult(len(rows), True)
    averages: dict[str, float | None] = {}
    for key in form.RATINGS:
        values = [r.answers[key] for r in rows if isinstance(r.answers.get(key), int)]
        averages[key] = round(mean(values), 2) if values else None
    recommend = dict.fromkeys(form.RECOMMEND, 0)
    for r in rows:
        choice = r.answers.get("recommend")
        if choice in recommend:
            recommend[choice] += 1
    return GroupResult(len(rows), False, averages, recommend)


async def aggregate_results(
    session: AsyncSession,
    *,
    job_id: uuid.UUID | None = None,
    since: date | None = None,
    min_group: int = MIN_GROUP,
) -> SurveyResults:
    """Candidate experience results for the session's organisation, for reports and the
    analytics agent: counts and averages only, never a single response; any group
    smaller than ``min_group`` is suppressed."""
    query = select(HireSurveyResponse)
    invites = select(HireSurveyInvite).where(HireSurveyInvite.status != "skipped")
    if job_id is not None:
        query = query.where(HireSurveyResponse.job_id == job_id)
        invites = invites.where(HireSurveyInvite.job_id == job_id)
    if since is not None:
        query = query.where(HireSurveyResponse.answered_on >= since)
        invites = invites.where(
            HireSurveyInvite.created_at >= datetime.combine(since, datetime.min.time(), UTC)
        )
    rows = list(await session.scalars(query))
    invited = len(list(await session.scalars(invites)))
    by_outcome = {
        outcome: _group([r for r in rows if r.outcome == outcome], min_group=min_group)
        for outcome in ("hired", "rejected", "withdrawn")
    }
    return SurveyResults(
        invited=invited,
        responses=len(rows),
        response_rate=round(len(rows) / invited, 3) if invited else None,
        overall=_group(rows, min_group=min_group),
        by_outcome=by_outcome,
        min_group=min_group,
    )


@dataclass(frozen=True, slots=True)
class NamedResponse:
    response_id: uuid.UUID
    application_id: uuid.UUID
    candidate_id: uuid.UUID
    outcome: str
    answers: dict[str, Any]
    comment: str | None
    answered_on: date


async def named_responses(
    session: AsyncSession, *, job_id: uuid.UUID | None = None
) -> list[NamedResponse]:
    """Only the responses whose authors chose to attach their name."""
    query = select(HireSurveyResponse).where(HireSurveyResponse.named.is_(True))
    if job_id is not None:
        query = query.where(HireSurveyResponse.job_id == job_id)
    out: list[NamedResponse] = []
    for r in await session.scalars(query.order_by(HireSurveyResponse.answered_on.desc())):
        if r.candidate_id is None or r.application_id is None:
            continue
        comment = read_sealed(r, "comment")
        out.append(
            NamedResponse(
                r.id,
                r.application_id,
                r.candidate_id,
                r.outcome,
                dict(r.answers),
                str(comment) if comment else None,
                r.answered_on,
            )
        )
    return out
