# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Offers: build, approve, send, and the candidate's answer (brief 3.8.3).

* ``create_offer`` / ``update_offer`` (``hire.offer.create``): from a template, with the
  compensation (encrypted), the start date and the conditions. Editable while ``draft``
  or ``changes_requested``; each edit raises ``version``.
* ``submit_offer``: ``pending_approval``; the API then starts the ``hire_offer`` run as
  the submitter, so the approval engine excludes them from every step.
* After the chain approved: ``send_offer`` drafts the offer email (the sensitive
  ``hire.candidate.offer`` template, with the portal link) and submits it; the email
  goes out only after its own approval (sensitive template gating). Then the offer is
  visible to the candidate in the portal (``visible_to_candidate``).
* ``respond`` (the candidate, in the portal): accept or decline, once, recorded as a
  decision record (actor ``candidate``) and audited.
* ``withdraw``: the hiring team takes back an offer the candidate has not answered.

Compensation is shown only to people with ``hire.offer.compensation``. The approval
payload carries the annual total (the chain's threshold reads it) and a hash binding
the exact compensation and conditions; the full breakdown stays encrypted here.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from pairly_core.audit import ActorType, append_audit
from pairly_core.crypto.types import seal
from pairly_core.decisions.models import DecisionKind
from pairly_core.decisions.service import DecisionDraft, record_decision
from pairly_core.errors import BadRequestError, ConflictError, ForbiddenError, NotFoundError
from pairly_hire.links import LinkConfig
from pairly_hire.messaging import compose
from pairly_hire.messaging.compose import Author
from pairly_hire.messaging.library import OFFER as OFFER_TEMPLATE
from pairly_hire.messaging.models import HireMessage, MessageStatus
from pairly_hire.offers.models import EDITABLE, LIVE, HireOffer, OfferStatus, as_list
from pairly_hire.offers.templates import BY_KEY, OfferTemplate
from pairly_hire.pipeline.records import AppRecord, load_record, organisation_name
from pairly_hire.sealing import read_sealed

WORKFLOW: Final = "hire"
ENTITY: Final = "hire.offer"
SYSTEM_ACTOR: Final = "pairly.hire.offer_graph"
MAX_CONDITIONS: Final = 12
MAX_CONDITION: Final = 300
MAX_AMOUNT: Final = Decimal("10000000")
_REF_ALPHABET: Final = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
MONTHLY: Final = ("base_monthly", "housing_monthly", "transport_monthly", "other_monthly")


def new_reference() -> str:
    return "OFR-" + "".join(secrets.choice(_REF_ALPHABET) for _ in range(6))


def _money(value: Any, name: str, *, required: bool = False) -> Decimal:
    if value in (None, ""):
        if required:
            raise BadRequestError(f"{name}: an amount is needed")
        return Decimal("0")
    try:
        amount = Decimal(str(value))
    except InvalidOperation:
        raise BadRequestError(f"{name}: a number") from None
    if not amount.is_finite() or amount < 0 or amount > MAX_AMOUNT:
        raise BadRequestError(f"{name}: between 0 and {MAX_AMOUNT}")
    return amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def clean_compensation(raw: dict[str, Any]) -> dict[str, str]:
    """Monthly amounts as strings with 2 decimals, the bonus target and the annual total
    (12 times the monthly amounts). Canonical: what the approval's hash binds."""
    currency = str(raw.get("currency") or "AED").upper()
    if len(currency) != 3 or not currency.isalpha() or not currency.isascii():
        raise BadRequestError("currency: a 3 letter code")
    out: dict[str, str] = {"currency": currency}
    for name in MONTHLY:
        out[name] = f"{_money(raw.get(name), name, required=name == 'base_monthly'):.2f}"
    if Decimal(out["base_monthly"]) <= 0:
        raise BadRequestError("base_monthly: more than 0")
    bonus = _money(raw.get("bonus_target_pct"), "bonus_target_pct")
    if bonus > 100:
        raise BadRequestError("bonus_target_pct: 0 to 100")
    out["bonus_target_pct"] = f"{bonus:.2f}"
    total = sum((Decimal(out[n]) for n in MONTHLY), Decimal("0")) * 12
    out["annual_total"] = f"{total:.2f}"
    return out


def clean_conditions(raw: list[Any]) -> list[str]:
    if len(raw) > MAX_CONDITIONS:
        raise BadRequestError(f"at most {MAX_CONDITIONS} conditions")
    out: list[str] = []
    for item in raw:
        text = " ".join(str(item).split())
        if not text:
            continue
        if len(text) > MAX_CONDITION:
            raise BadRequestError(f"a condition has at most {MAX_CONDITION} characters")
        out.append(text)
    return out


def _seal(row: HireOffer, column: str, value: Any) -> Any:
    return seal(HireOffer, column, value, org_id=row.org_id, row_id=row.id)


def compensation_of(row: HireOffer) -> dict[str, str]:
    value = read_sealed(row, "compensation")
    return {str(k): str(v) for k, v in value.items()} if isinstance(value, dict) else {}


def template_of(row: HireOffer) -> OfferTemplate:
    return BY_KEY.get(row.template_key) or BY_KEY["standard"]


def _digest(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def payload_of(row: HireOffer) -> dict[str, Any]:
    """What the approval covers (canonical JSON: strings and integers only). The
    breakdown stays encrypted: the payload binds it by hash."""
    comp = compensation_of(row)
    return {
        "offer_id": str(row.id),
        "reference": row.reference,
        "application_id": str(row.application_id),
        "job_title": row.job_title,
        "start_date": row.start_date.isoformat(),
        "template": row.template_key,
        "currency": comp.get("currency", ""),
        "annual_total": comp.get("annual_total", "0.00"),
        "compensation_hash": _digest(comp),
        "conditions_hash": _digest(as_list(row.conditions)),
        "version": int(row.version),
        "submitted_by": str(row.submitted_by) if row.submitted_by else "",
    }


async def audit_offer(
    session: AsyncSession,
    row: HireOffer,
    action: str,
    *,
    actor_type: ActorType,
    actor_id: str,
    extra: dict[str, Any] | None = None,
) -> None:
    await append_audit(
        session,
        action=action,
        actor_type=actor_type,
        actor_id=actor_id,
        entity_type=ENTITY,
        entity_id=row.id,
        workflow=WORKFLOW,
        payload={
            "reference": row.reference,
            "application_id": str(row.application_id),
            "status": row.status,
            "version": int(row.version),
        }
        | (extra or {}),
    )


async def get_offer(session: AsyncSession, offer_id: uuid.UUID, *, lock: bool = False) -> HireOffer:
    query = select(HireOffer).where(HireOffer.id == offer_id)
    if lock:
        query = query.with_for_update()
    row = await session.scalar(query)
    if row is None:
        raise NotFoundError("offer not found")
    return row


async def locked(session: AsyncSession, offer_id: str | uuid.UUID) -> HireOffer:
    return await get_offer(session, uuid.UUID(str(offer_id)), lock=True)


async def offers_for(session: AsyncSession, application_id: uuid.UUID) -> list[HireOffer]:
    return list(
        await session.scalars(
            select(HireOffer)
            .where(HireOffer.application_id == application_id)
            .order_by(HireOffer.created_at.desc(), HireOffer.id)
        )
    )


def _start(value: date) -> date:
    if not isinstance(value, date):
        raise BadRequestError("start_date: a date")
    if value < datetime.now(UTC).date():
        raise ConflictError("the start date must be today or later")
    return value


async def create_offer(
    session: AsyncSession,
    actor_id: uuid.UUID,
    application_id: uuid.UUID,
    *,
    template_key: str,
    start_date: date,
    compensation: dict[str, Any],
    conditions: list[Any] | None,
    locale: str,
    job_title: str | None = None,
) -> HireOffer:
    record = await load_record(session, application_id)
    if record.candidate.erased_at is not None:
        raise ConflictError("this candidate's data was erased")
    template = BY_KEY.get(template_key)
    if template is None:
        raise BadRequestError("choose an offer template")
    if any(r.status in LIVE for r in await offers_for(session, application_id)):
        raise ConflictError("this candidate already has an open offer")
    lang = "ar" if locale == "ar" else "en"
    defaults = template.conditions_ar if lang == "ar" else template.conditions
    title = " ".join((job_title or "").split()) or (
        record.job.title_ar if lang == "ar" and record.job.title_ar else record.job.title
    )
    now = datetime.now(UTC)
    row = HireOffer(
        id=uuid.uuid4(),
        org_id=record.app.org_id,
        application_id=record.app.id,
        candidate_id=record.candidate.id,
        job_id=record.job.id,
        reference=new_reference(),
        template_key=template.key,
        locale=lang,
        job_title=title[:160],
        start_date=_start(start_date),
        conditions=clean_conditions(list(defaults) if conditions is None else conditions),
        status=OfferStatus.DRAFT.value,
        version=1,
        created_by=actor_id,
        created_at=now,
        updated_at=now,
    )
    row.compensation = _seal(row, "compensation", clean_compensation(compensation))
    session.add(row)
    await session.flush()
    await audit_offer(
        session,
        row,
        "hire.offer.drafted",
        actor_type=ActorType.USER,
        actor_id=str(actor_id),
        extra={"template": template.key},
    )
    return row


async def update_offer(
    session: AsyncSession,
    row: HireOffer,
    actor_id: uuid.UUID,
    *,
    start_date: date | None,
    compensation: dict[str, Any] | None,
    conditions: list[Any] | None,
    job_title: str | None,
    expected_version: int,
) -> HireOffer:
    if row.status not in EDITABLE:
        raise ConflictError("only a draft or an offer sent back for changes can be edited")
    if row.version != expected_version:
        raise ConflictError("someone else changed this offer; reload it")
    if start_date is not None:
        row.start_date = _start(start_date)
    if compensation is not None:
        row.compensation = _seal(row, "compensation", clean_compensation(compensation))
    if conditions is not None:
        row.conditions = clean_conditions(conditions)
    if job_title is not None and job_title.strip():
        row.job_title = " ".join(job_title.split())[:160]
    row.version += 1
    row.updated_at = datetime.now(UTC)
    await session.flush()
    await audit_offer(
        session, row, "hire.offer.edited", actor_type=ActorType.USER, actor_id=str(actor_id)
    )
    return row


async def submit_offer(session: AsyncSession, row: HireOffer, actor_id: uuid.UUID) -> HireOffer:
    if row.status not in EDITABLE:
        raise ConflictError("this offer is already submitted")
    row.status = OfferStatus.PENDING_APPROVAL.value
    row.status_reason = None
    row.submitted_by = actor_id
    row.approval_id = None
    row.run_id = None
    row.updated_at = datetime.now(UTC)
    await session.flush()
    await audit_offer(
        session, row, "hire.offer.submitted", actor_type=ActorType.USER, actor_id=str(actor_id)
    )
    return row


def offer_url(links: LinkConfig, row: HireOffer) -> str:
    """The candidate's offer page in the portal (they sign in with a magic link)."""
    if not links.web_url:
        raise ConflictError("links are not configured")
    return f"{links.web_url}/{row.locale}/portal/applications/{row.application_id}/offer"


async def send_offer(
    session: AsyncSession,
    row: HireOffer,
    record: AppRecord,
    author: Author,
    *,
    links: LinkConfig,
) -> HireMessage:
    """Draft and submit the offer email; the caller starts its approval run."""
    if row.status != OfferStatus.APPROVED.value:
        raise ConflictError("an offer is sent only after its approval")
    message = await compose.create_draft(
        session,
        record,
        author,
        template_key=OFFER_TEMPLATE,
        values={"offer_url": offer_url(links, row)},
        links=links,
        locale=row.locale,
    )
    await compose.submit_message(session, message, record, author, links=links)
    row.status = OfferStatus.SENDING.value
    row.message_id = message.id
    row.updated_at = datetime.now(UTC)
    await session.flush()
    await audit_offer(
        session,
        row,
        "hire.offer.send_requested",
        actor_type=author.type,
        actor_id=author.id,
        extra={"message_id": str(message.id)},
    )
    return message


async def message_sent(session: AsyncSession, row: HireOffer) -> bool:
    """The offer email was approved and handed to delivery (queued or sent)."""
    if row.message_id is None:
        return False
    status = await session.scalar(
        select(HireMessage.status).where(HireMessage.id == row.message_id)
    )
    return status in (MessageStatus.QUEUED.value, MessageStatus.SENT.value)


async def visible_to_candidate(session: AsyncSession, row: HireOffer) -> bool:
    """The candidate sees an offer once its email was sent (after both approvals)."""
    if row.erased_at is not None:
        return False
    if row.status in (OfferStatus.ACCEPTED.value, OfferStatus.DECLINED.value):
        return True
    return row.status == OfferStatus.SENDING.value and await message_sent(session, row)


async def candidate_has_offer(
    session: AsyncSession, candidate_id: uuid.UUID, application_id: uuid.UUID
) -> bool:
    for row in await offers_for(session, application_id):
        if row.candidate_id == candidate_id and await visible_to_candidate(session, row):
            return True
    return False


async def withdraw(session: AsyncSession, row: HireOffer, actor_id: uuid.UUID) -> HireOffer:
    allowed = (
        *EDITABLE,
        OfferStatus.PENDING_APPROVAL.value,
        OfferStatus.APPROVED.value,
        OfferStatus.SENDING.value,
    )
    if row.status not in allowed:
        raise ConflictError("this offer can no longer be withdrawn")
    row.status = OfferStatus.WITHDRAWN.value
    row.status_reason = "withdrawn"
    row.updated_at = datetime.now(UTC)
    await session.flush()
    await audit_offer(
        session, row, "hire.offer.withdrawn", actor_type=ActorType.USER, actor_id=str(actor_id)
    )
    return row


async def respond(
    session: AsyncSession,
    row: HireOffer,
    candidate_id: uuid.UUID,
    *,
    accept: bool,
    note: str | None,
) -> HireOffer:
    """The candidate's answer, once, recorded as a decision record and audited."""
    if row.candidate_id != candidate_id:
        raise ForbiddenError("this is not your offer")
    if not await visible_to_candidate(session, row):
        raise NotFoundError("offer not found")
    if row.status != OfferStatus.SENDING.value:
        raise ConflictError("you have already answered this offer")
    now = datetime.now(UTC)
    outcome = OfferStatus.ACCEPTED if accept else OfferStatus.DECLINED
    clean_note = " ".join((note or "").split())[:1000] or None
    actor = f"hire_candidate:{candidate_id}"
    stored = await record_decision(
        session,
        DecisionDraft(
            kind=DecisionKind.APPROVAL,
            workflow=WORKFLOW,
            entity_type=ENTITY,
            entity_id=str(row.id),
            actor_type=ActorType.CANDIDATE,
            actor_id=actor,
            inputs={
                "offer_id": str(row.id),
                "reference": row.reference,
                "version": int(row.version),
                "compensation_hash": _digest(compensation_of(row)),
                "conditions_hash": _digest(as_list(row.conditions)),
            },
            model_call_ids=[],
            output={"decision": outcome.value},
            evidence={"approval_id": str(row.approval_id) if row.approval_id else None},
            reason=clean_note or ("Accepted the offer." if accept else "Declined the offer."),
            decided_at=now,
            supersedes=None,
        ),
    )
    row.status = outcome.value
    row.responded_at = now
    row.response_record_id = stored.id
    if clean_note:
        row.response_note = _seal(row, "response_note", clean_note)
    row.updated_at = now
    await session.flush()
    await audit_offer(
        session,
        row,
        f"hire.offer.{outcome.value}",
        actor_type=ActorType.CANDIDATE,
        actor_id=actor,
        extra={"decision_record_id": stored.id},
    )
    return row


@dataclass(frozen=True, slots=True)
class LetterData:
    """Everything the offer letter (PDF) shows."""

    company: str
    candidate_name: str
    reference: str
    job_title: str
    start_date: date
    compensation: dict[str, str]
    conditions: list[str]
    template: OfferTemplate
    locale: str


async def letter(session: AsyncSession, row: HireOffer) -> LetterData:
    record = await load_record(session, row.application_id)
    return LetterData(
        company=await organisation_name(session, row.org_id),
        candidate_name=record.full_name,
        reference=row.reference,
        job_title=row.job_title,
        start_date=row.start_date,
        compensation=compensation_of(row),
        conditions=as_list(row.conditions),
        template=template_of(row),
        locale=row.locale,
    )
