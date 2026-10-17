# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Compose messages to candidates: draft, edit, preview, send or submit for approval.

A message is a template plus values (``hire_messages``). The values come from two
places only:

* the record (``RECORD_VARIABLES``: name, job title, company, reference, stage, the
  preferences link), filled here every time, never typed and never written by a model;
* a person (every other declared variable: interview details, an offer link, the
  personal paragraph, a reason). An AI draft may propose ``personal_note`` and, for a
  rejection with screening evidence, ``reason`` (``pairly_hire.messaging.drafting``);
  the person sees and edits them before anything happens.

Undeclared names are dropped, so nothing outside the template can reach the email.

``preview`` renders exactly what the candidate will get (the engine's sandboxed
renderer, the template version that will be used, the candidate's language).

Sending:

* not sensitive (``requires_approval`` false on the template version): ``send_message``
  queues the email through the engine's ``notify`` (the worker delivers it). A status
  update to a candidate who turned updates off is not sent (``suppressed``);
  transactional messages are always sent.
* sensitive (rejections, offers): ``send_message`` refuses. ``submit_message`` marks the
  draft ``submitted``; the route then starts the ``hire_message`` graph
  (``pairly_hire.messaging.graph``), which creates the approval whose payload is the
  exact rendered email and sends it only after a person approved it.

Every change is audited; a sent or submitted message is never edited again.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from pairly_core.audit import ActorType, append_audit
from pairly_core.crypto.types import seal
from pairly_core.errors import BadRequestError, ConflictError, NotFoundError
from pairly_core.notifications.models import Template
from pairly_core.notifications.service import Recipient, notify
from pairly_core.notifications.templates import resolve_template, source_of
from pairly_core.notifications.templating import (
    Rendered,
    TemplateRenderError,
    VariableSpec,
    render_source,
)
from pairly_hire.links import LinkConfig, prefs_url
from pairly_hire.messaging import library
from pairly_hire.messaging.models import (
    EDITABLE,
    HireMessage,
    MessagePurpose,
    MessageStatus,
)
from pairly_hire.messaging.prefs import prefs_for
from pairly_hire.pipeline.records import AppRecord, ensure_entry, organisation_name
from pairly_hire.sealing import read_sealed

WORKFLOW: Final = "hire"
ENTITY: Final = "hire.message"
CHANNEL: Final = "email"
MAX_VALUE: Final = 2000


@dataclass(frozen=True, slots=True)
class Author:
    """Who drafted or changed a message (a person, or the system for the SLA job)."""

    type: ActorType
    id: str


@dataclass(frozen=True, slots=True)
class Preview:
    ok: bool
    subject: str | None
    text: str | None
    # Required person-filled fields that are still empty (``ok`` is false).
    missing: tuple[str, ...]
    error: str | None = None


def _user_id(author: Author) -> uuid.UUID | None:
    if author.type is not ActorType.USER:
        return None
    try:
        return uuid.UUID(author.id)
    except ValueError:
        return None


def _clean(value: Any) -> Any:
    if isinstance(value, str):
        return value.strip()[:MAX_VALUE]
    return value


async def template_for(session: AsyncSession, key: str, locale: str) -> Template:
    """The version that will be sent (the organisation's own version wins)."""
    if library.entry(key) is None:
        raise BadRequestError("this is not a candidate message template")
    template = await resolve_template(session, key=key, channel=CHANNEL, locale=locale)
    if template is None:
        raise NotFoundError("template not found")
    return template


def specs_of(template: Template) -> Mapping[str, VariableSpec]:
    return source_of(template).variables


def editable_names(specs: Mapping[str, VariableSpec]) -> list[str]:
    """Variables a person fills (everything declared but the record's)."""
    return sorted(n for n in specs if n not in library.RECORD_VARIABLES and n != "stage_label")


async def record_values(
    session: AsyncSession,
    record: AppRecord,
    specs: Mapping[str, VariableSpec],
    locale: str,
    links: LinkConfig,
) -> dict[str, Any]:
    """The record's values for the declared variables. Never typed, never AI written."""
    stage = record.stage
    values: dict[str, Any] = {
        "candidate_name": record.full_name,
        "job_title": (record.job.title_ar if locale == "ar" else "") or record.job.title,
        "company": await organisation_name(session, record.app.org_id),
        "reference": record.app.reference,
        "stage_label": (stage.label_ar if locale == "ar" else "") or stage.label,
    }
    if "preferences_url" in specs and links.ready:
        values["preferences_url"] = prefs_url(links, record.app.org_id, record.candidate.id, locale)
    return {k: v for k, v in values.items() if k in specs}


def person_values(
    specs: Mapping[str, VariableSpec], given: Mapping[str, Any] | None
) -> dict[str, Any]:
    """The person's values for editable declared variables; anything else is dropped."""
    allowed = set(editable_names(specs))
    out: dict[str, Any] = {}
    for name, value in (given or {}).items():
        if name not in allowed or value is None:
            continue
        cleaned = _clean(value)
        if cleaned == "":
            continue
        out[name] = cleaned
    return out


def stored_values(message: HireMessage) -> dict[str, Any]:
    value = read_sealed(message, "variables")
    return dict(value) if isinstance(value, dict) else {}


def _seal(message: HireMessage, column: str, value: Any) -> Any:
    return seal(HireMessage, column, value, org_id=message.org_id, row_id=message.id)


async def _render(
    session: AsyncSession,
    message: HireMessage,
    record: AppRecord,
    links: LinkConfig,
) -> tuple[Template, dict[str, Any], Rendered | None, Preview]:
    assert message.template_key is not None  # noqa: S101 (outbound messages)
    template = await template_for(session, message.template_key, message.locale)
    specs = specs_of(template)
    values = person_values(specs, stored_values(message)) | await record_values(
        session, record, specs, message.locale, links
    )
    missing = tuple(n for n in editable_names(specs) if specs[n].required and n not in values)
    if missing:
        return template, values, None, Preview(False, None, None, missing)
    try:
        rendered = render_source(source_of(template), values)
    except TemplateRenderError as exc:
        return template, values, None, Preview(False, None, None, (), error=str(exc)[:200])
    return template, values, rendered, Preview(True, rendered.subject, rendered.text, ())


async def preview(
    session: AsyncSession, message: HireMessage, record: AppRecord, links: LinkConfig
) -> Preview:
    _, _, _, result = await _render(session, message, record, links)
    return result


async def _audit(
    session: AsyncSession,
    author: Author,
    action: str,
    message: HireMessage,
    payload: dict[str, Any] | None = None,
) -> None:
    await append_audit(
        session,
        action=action,
        actor_type=author.type,
        actor_id=author.id,
        entity_type=ENTITY,
        entity_id=message.id,
        workflow=WORKFLOW,
        payload={
            "application_id": str(message.application_id),
            "template_key": message.template_key,
            "status": message.status,
            "purpose": message.purpose,
            **(payload or {}),
        },
    )


async def create_draft(
    session: AsyncSession,
    record: AppRecord,
    author: Author,
    *,
    template_key: str,
    purpose: MessagePurpose = MessagePurpose.MANUAL,
    values: Mapping[str, Any] | None = None,
    links: LinkConfig,
    locale: str | None = None,
) -> HireMessage:
    """A new draft for ``record``'s candidate. Audited (``hire.message.drafted``)."""
    lang = locale if locale in {"en", "ar"} else record.locale
    template = await template_for(session, template_key, lang)
    specs = specs_of(template)
    now = datetime.now(UTC)
    message = HireMessage(
        id=uuid.uuid4(),
        org_id=record.app.org_id,
        application_id=record.app.id,
        candidate_id=record.candidate.id,
        direction="outbound",
        template_key=template_key,
        locale=lang,
        purpose=purpose.value,
        sensitive=template.requires_approval,
        status=MessageStatus.DRAFT.value,
        ai_fields=[],
        created_by_type=author.type.value,
        created_by_id=author.id,
        created_at=now,
        updated_at=now,
    )
    message.variables = _seal(message, "variables", person_values(specs, values))
    _, _, rendered, _ = await _render(session, message, record, links)
    if rendered is not None:
        message.rendered = _seal(
            message, "rendered", {"subject": rendered.subject, "text": rendered.text}
        )
    session.add(message)
    await session.flush()
    await _audit(session, author, "hire.message.drafted", message, {"sensitive": message.sensitive})
    return message


async def get_message(
    session: AsyncSession, message_id: uuid.UUID, *, lock: bool = False
) -> HireMessage:
    query = select(HireMessage).where(HireMessage.id == message_id)
    if lock:
        query = query.with_for_update()
    message = await session.scalar(query)
    if message is None:
        raise NotFoundError("message not found")
    return message


def _editable(message: HireMessage) -> None:
    if message.status not in EDITABLE:
        raise ConflictError("this message was already sent or submitted; it cannot change")


async def update_draft(
    session: AsyncSession,
    message: HireMessage,
    record: AppRecord,
    author: Author,
    *,
    values: Mapping[str, Any],
    links: LinkConfig,
    ai_fields: list[str] | None = None,
    model_call_id: uuid.UUID | None = None,
    reason_source: str | None = None,
) -> Preview:
    """Replace the person's values (missing names are cleared). Audited."""
    _editable(message)
    assert message.template_key is not None  # noqa: S101
    template = await template_for(session, message.template_key, message.locale)
    clean = person_values(specs_of(template), values)
    previous = stored_values(message)
    message.variables = _seal(message, "variables", clean)
    if ai_fields is not None:
        message.ai_fields = sorted(set(ai_fields) & set(clean))
        message.model_call_id = model_call_id
        message.reason_source = reason_source if "reason" in clean else None
    else:
        # A person's edit: fields they changed are theirs now, not the model's.
        kept = [f for f in message.ai_fields if clean.get(f) == previous.get(f)]
        message.ai_fields = kept
        if "reason" not in kept:
            message.reason_source = None
    message.updated_at = datetime.now(UTC)
    _, _, rendered, result = await _render(session, message, record, links)
    message.rendered = (
        _seal(message, "rendered", {"subject": rendered.subject, "text": rendered.text})
        if rendered is not None
        else None
    )
    await session.flush()
    await _audit(
        session,
        author,
        "hire.message.ai_drafted" if ai_fields is not None else "hire.message.edited",
        message,
        {
            "fields": sorted(clean),
            "ai_fields": list(message.ai_fields),
            "model_call_id": str(model_call_id) if model_call_id else None,
        },
    )
    return result


async def cancel_draft(session: AsyncSession, message: HireMessage, author: Author) -> None:
    _editable(message)
    message.status = MessageStatus.CANCELLED.value
    message.updated_at = datetime.now(UTC)
    await session.flush()
    await _audit(session, author, "hire.message.cancelled", message)


async def mark_updated(record: AppRecord, session: AsyncSession, when: datetime) -> None:
    """The candidate heard from us: the anti-ghosting clock starts again."""
    entry = await ensure_entry(session, record)
    entry.last_update_at = when
    entry.sla_drafted_at = None
    entry.updated_at = when
    await session.flush()


async def send_message(
    session: AsyncSession,
    message: HireMessage,
    record: AppRecord,
    author: Author,
    *,
    links: LinkConfig,
) -> HireMessage:
    """Queue a non-sensitive message (see the module docstring). Audited."""
    _editable(message)
    template, values, rendered, result = await _render(session, message, record, links)
    if template.requires_approval:
        raise ConflictError("this message needs an approval: submit it for approval")
    if rendered is None:
        raise BadRequestError(
            "fill in: " + ", ".join(result.missing) if result.missing else "the message is invalid"
        )
    now = datetime.now(UTC)
    message.rendered = _seal(
        message, "rendered", {"subject": rendered.subject, "text": rendered.text}
    )
    message.updated_at = now
    entry = library.entry(template.key)
    transactional = entry.transactional if entry is not None else True
    if not transactional and not (await prefs_for(session, record.candidate.id)).updates_email:
        message.status = MessageStatus.SUPPRESSED.value
        message.status_reason = "candidate_opted_out"
        await session.flush()
        await _audit(session, author, "hire.message.suppressed", message, {"reason": "opted_out"})
        return message
    results = await notify(
        session,
        recipient=Recipient.external(record.email, locale=message.locale),
        template_key=template.key,
        variables=values,
        dedupe_key=f"hire.msg:{message.id}",
        channels=[CHANNEL],
        workflow=WORKFLOW,
        actor_type=author.type,
        actor_id=author.id,
    )
    sent = next((r for r in results if r.channel == CHANNEL), None)
    if sent is None or sent.notification_id is None:
        message.status = MessageStatus.FAILED.value
        message.status_reason = (sent.reason if sent else "no_template") or "not_queued"
        await session.flush()
        await _audit(
            session, author, "hire.message.failed", message, {"reason": message.status_reason}
        )
        return message
    message.status = MessageStatus.QUEUED.value
    message.notification_id = sent.notification_id
    message.sent_at = now
    await mark_updated(record, session, now)
    await session.flush()
    await _audit(
        session,
        author,
        "hire.message.sent",
        message,
        {"notification_id": str(sent.notification_id)},
    )
    return message


async def submit_message(
    session: AsyncSession,
    message: HireMessage,
    record: AppRecord,
    author: Author,
    *,
    links: LinkConfig,
) -> HireMessage:
    """Mark a sensitive draft ``submitted``; the caller starts the approval graph."""
    _editable(message)
    template, values, rendered, result = await _render(session, message, record, links)
    if not template.requires_approval:
        raise ConflictError("this message does not need an approval: send it")
    if rendered is None:
        raise BadRequestError(
            "fill in: " + ", ".join(result.missing) if result.missing else "the message is invalid"
        )
    # Freeze every value (the record's too): the approver approves exactly this text,
    # and the email sent after the approval is rendered from the same values.
    message.variables = _seal(message, "variables", values)
    message.sensitive = True
    message.submitted_by = _user_id(author)
    message.status = MessageStatus.SUBMITTED.value
    message.status_reason = None
    message.rendered = _seal(
        message, "rendered", {"subject": rendered.subject, "text": rendered.text}
    )
    message.updated_at = datetime.now(UTC)
    await session.flush()
    await _audit(session, author, "hire.message.submitted", message)
    return message


def frozen_values(message: HireMessage) -> dict[str, Any]:
    """The values a submitted message was frozen with (``submit_message``)."""
    if message.status in EDITABLE:
        raise ConflictError("the message is still a draft")
    return stored_values(message)
