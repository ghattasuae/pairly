# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""``notify``: render a template for one recipient and queue it on each channel.

Runs inside the caller's transaction (tenant-bound session) and never talks to the
network: rows are written with status ``queued`` (or ``digest``, ``suppressed``, or
``sent`` for in-app items, which are the feed itself) and the worker delivers them
(``pairly_core.notifications.delivery``). So a notification commits or rolls back with
the change that caused it.

Per channel, in order:

1. Idempotency: ``<dedupe_key>:<channel>`` is unique per organisation; a repeat call
   returns the existing row (``replayed``) and writes nothing.
2. The template version (``resolve_template``), in the recipient's locale.
3. The address: the user's email, their verified Slack link (and the organisation's
   active Slack integration), or none for in-app. Missing: ``suppressed``.
4. Rendering in the sandbox, with the recipient's time zone.
5. Sensitive templates (``requires_approval``): refused unless ``approval_id`` names an
   approval that is ``approved``, whose ``payload_hash`` equals the hash of exactly
   this rendered message (``message_payload``), whose decision rows still prove it
   (the executor's checks, ``pairly_core.approvals.executor``), and that was never used
   for another message. One approval, one message, one channel. Workflows build the
   approval payload with ``sensitive_message_payload``, so the approver sees the exact
   text that will be sent, and any change to the template or the values afterwards
   breaks the hash and blocks the send.
   A workflow approval may also bind one message (``allow_bound_message``): for a
   template key registered with that approval type, the approval's own payload carries
   ``message_sha256`` (the ``message_hash`` of exactly this rendered message), and its
   stored payload must still hash to its ``payload_hash``. So one approval chain (an
   offer) authorises its exact email once, and nothing else.
6. Preferences (``pairly_core.notifications.preferences.plan``).
7. The row (content encrypted) and an audit event.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from pairly_core.approvals.executor import ExecutorRefusedError, _decisions_valid
from pairly_core.approvals.models import Approval, ApprovalStatus
from pairly_core.approvals.service import db_now
from pairly_core.audit import ActorType, append_audit
from pairly_core.crypto.blind_index import blind_index
from pairly_core.crypto.types import seal
from pairly_core.db import current_org, outside_portal
from pairly_core.errors import BadRequestError, ForbiddenError
from pairly_core.integrations.slack.blocks import SlackButton, message_blocks
from pairly_core.integrations.slack.links import active_slack, slack_link_for_user
from pairly_core.models import User
from pairly_core.notifications.email import MAX_ATTACHMENTS, Attachment
from pairly_core.notifications.links import is_app_path
from pairly_core.notifications.models import (
    Notification,
    NotificationPreference,
    NotificationStatus,
    Template,
    Urgency,
)
from pairly_core.notifications.preferences import (
    Preferences,
    category_policy,
    plan,
    preferences_from_row,
)
from pairly_core.notifications.templates import resolve_template, source_of
from pairly_core.notifications.templating import CHANNELS, LOCALES, Rendered, render_source
from pairly_core.rbac import RoleRegistry

log = structlog.get_logger(__name__)

NOTIFY_ACTOR: Final = "pairly.notifications"
MAX_DEDUPE_KEY: Final = 180


class NotificationActions:
    QUEUED = "core.notification.queued"
    HELD = "core.notification.held_for_digest"
    SUPPRESSED = "core.notification.suppressed"
    SENT = "core.notification.sent"
    FAILED = "core.notification.failed"
    RETRY = "core.notification.retry_scheduled"
    READ = "core.notification.read"
    PREFERENCES_CHANGED = "core.notification.preferences_changed"
    UNSUBSCRIBED = "core.notification.unsubscribed"
    SENSITIVE_REFUSED = "core.notification.sensitive_refused"
    RECEIPT_SKIPPED = "core.notification.receipt_skipped"


# Template key -> approval types whose payload may bind one rendered message of it.
_BOUND_MESSAGES: dict[str, frozenset[str]] = {}
BOUND_MESSAGE_KEY: Final = "message_sha256"


def allow_bound_message(template_key: str, approval_type: str) -> None:
    """Let an approval of ``approval_type`` authorise one message of ``template_key``
    whose ``message_hash`` its payload carries as ``message_sha256``. Plugins call this
    at import (idempotent)."""
    _BOUND_MESSAGES[template_key] = _BOUND_MESSAGES.get(template_key, frozenset()) | {approval_type}


def bound_message_types(template_key: str) -> frozenset[str]:
    return _BOUND_MESSAGES.get(template_key, frozenset())


class SensitiveTemplateError(ForbiddenError):
    """A sensitive template without a matching, approved approval."""

    code = "approval_required"
    title = "This message needs an approval before it is sent"

    def __init__(self, reason: str) -> None:
        super().__init__(f"sensitive message refused: {reason}")
        self.reason = reason


@dataclass(frozen=True, slots=True)
class Recipient:
    """A Pairly user (preferences apply) or an external email address (transactional)."""

    user_id: uuid.UUID | None = None
    email: str | None = None
    locale: str = "en"
    timezone: str | None = None

    @classmethod
    def user(cls, user_id: uuid.UUID) -> Recipient:
        return cls(user_id=user_id)

    @classmethod
    def external(cls, email: str, *, locale: str = "en", timezone: str | None = None) -> Recipient:
        return cls(
            email=email.strip(), locale=locale if locale in LOCALES else "en", timezone=timezone
        )

    def __post_init__(self) -> None:
        if (self.user_id is None) == (self.email is None):
            raise BadRequestError("a recipient is a user or an email address")


@dataclass(frozen=True, slots=True)
class NotifyResult:
    channel: str
    status: str
    reason: str | None
    notification_id: uuid.UUID | None
    replayed: bool = False


@dataclass(frozen=True, slots=True)
class Extras:
    """Channel extras that are not template text."""

    # A link for the in-app item and the digest line: an app path without a locale
    # (``/inbox?item=<id>``, ``pairly_core.notifications.links``), never a full URL and
    # never a single-use token.
    link: str | None = None
    slack_buttons: Sequence[SlackButton] = ()
    slack_note: str | None = None
    # Email: reply threading and the RFC 3834 header value.
    in_reply_to: str | None = None
    auto_submitted: str = "auto-generated"
    # Email: text attachments (a calendar invitation), stored encrypted with the content.
    attachments: Sequence[Attachment] = ()
    extra: Mapping[str, Any] = field(default_factory=dict)


def message_hash(payload: Mapping[str, Any]) -> str:
    """SHA-256 of canonical JSON: the same function as ``approvals.service.canonical_payload``."""
    raw = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def recipient_ref(recipient: Recipient, address: str | None) -> str:
    if recipient.user_id is not None:
        return f"user:{recipient.user_id}"
    return f"email:{(address or '').lower()}"


def message_payload(
    *, template: Template, channel: str, recipient: str, rendered: Rendered
) -> dict[str, Any]:
    """What a sensitive message approval covers: the exact message, for one recipient."""
    return {
        "kind": "notification",
        "template_key": template.key,
        "template_id": str(template.id),
        "channel": channel,
        "locale": rendered.locale,
        "recipient": recipient,
        "subject": rendered.subject,
        "text": rendered.text,
        "html_sha256": hashlib.sha256(rendered.html.encode("utf-8")).hexdigest()
        if rendered.html
        else None,
    }


async def _preferences(session: AsyncSession, user_id: uuid.UUID) -> Preferences:
    row = await session.scalar(
        select(NotificationPreference).where(NotificationPreference.user_id == user_id)
    )
    return preferences_from_row(row)


async def _render(
    session: AsyncSession,
    *,
    template_key: str,
    channel: str,
    locale: str,
    variables: Mapping[str, Any],
    tz: str | None,
) -> tuple[Template, Rendered] | None:
    template = await resolve_template(session, key=template_key, channel=channel, locale=locale)
    if template is None:
        return None
    return template, render_source(source_of(template), variables, tz=tz)


async def sensitive_message_payload(
    session: AsyncSession,
    *,
    recipient: Recipient,
    template_key: str,
    variables: Mapping[str, Any],
    channel: str = "email",
) -> dict[str, Any]:
    """The approval payload for a sensitive message (``request_approval(payload=...)``)."""
    locale, tz, address = await _recipient_basics(session, recipient, channel)
    found = await _render(
        session,
        template_key=template_key,
        channel=channel,
        locale=locale,
        variables=variables,
        tz=tz,
    )
    if found is None:
        raise BadRequestError("no template for this key and channel")
    template, rendered = found
    return message_payload(
        template=template,
        channel=channel,
        recipient=recipient_ref(recipient, address),
        rendered=rendered,
    )


async def _recipient_basics(
    session: AsyncSession, recipient: Recipient, channel: str
) -> tuple[str, str | None, str | None]:
    """Locale, time zone and email address (for the email channel)."""
    if recipient.user_id is None:
        return recipient.locale, recipient.timezone, recipient.email
    prefs = await _preferences(session, recipient.user_id)
    email = None
    if channel == "email":
        email = await session.scalar(select(User.email).where(User.id == recipient.user_id))
    return prefs.locale, prefs.timezone, email


def _binds(approval: Approval, template_key: str, digest: str) -> bool:
    """The approval covers this message: its payload is the message itself, or (for a
    registered approval type) its intact payload names this message's hash."""
    if approval.payload_hash == digest:
        return True
    if approval.approval_type not in bound_message_types(template_key):
        return False
    payload = approval.payload if isinstance(approval.payload, dict) else {}
    if payload.get(BOUND_MESSAGE_KEY) != digest:
        return False
    try:
        return message_hash(payload) == approval.payload_hash
    except (TypeError, ValueError):
        return False


async def _check_sensitive(
    session: AsyncSession,
    registry: RoleRegistry | None,
    approval_id: uuid.UUID | None,
    digest: str,
    template_key: str,
) -> Approval:
    if approval_id is None:
        raise SensitiveTemplateError("approval_missing")
    if registry is None:
        raise SensitiveTemplateError("no_role_registry")
    approval = await session.scalar(
        select(Approval).where(Approval.id == approval_id).with_for_update()
    )
    if approval is None:
        raise SensitiveTemplateError("approval_not_found")
    if approval.status != ApprovalStatus.APPROVED.value:
        raise SensitiveTemplateError("approval_not_approved")
    if not _binds(approval, template_key, digest):
        raise SensitiveTemplateError("payload_hash_mismatch")
    try:
        await _decisions_valid(session, registry, approval)
    except ExecutorRefusedError as exc:
        raise SensitiveTemplateError(exc.reason) from None
    used = await session.scalar(
        select(Notification.id).where(Notification.approval_id == approval.id)
    )
    if used is not None:
        raise SensitiveTemplateError("approval_already_used")
    return approval


async def audit_notification(
    session: AsyncSession,
    action: str,
    row: Notification,
    *,
    actor_type: ActorType,
    actor_id: str,
) -> None:
    await append_audit(
        session,
        action=action,
        actor_type=actor_type,
        actor_id=actor_id,
        entity_type="notification",
        entity_id=row.id,
        workflow=row.workflow,
        payload={
            "channel": row.channel,
            "category": row.category,
            "template_key": row.template_key,
            "template_id": str(row.template_id) if row.template_id else None,
            "status": row.status,
            "reason": row.reason,
            "urgency": row.urgency,
            "recipient_user_id": str(row.recipient_user_id) if row.recipient_user_id else None,
            "approval_id": str(row.approval_id) if row.approval_id else None,
            "content_hash": row.content_hash,
        },
    )


async def record_sensitive_refusal(
    session: AsyncSession,
    *,
    template_key: str,
    approval_id: uuid.UUID | None,
    reason: str,
    workflow: str | None = None,
) -> None:
    """Audit a refused sensitive message. Call in a fresh transaction after the rollback."""
    await append_audit(
        session,
        action=NotificationActions.SENSITIVE_REFUSED,
        actor_type=ActorType.SYSTEM,
        actor_id=NOTIFY_ACTOR,
        entity_type="approval" if approval_id else "notification",
        entity_id=approval_id,
        workflow=workflow,
        payload={"template_key": template_key, "reason": reason},
    )


async def notify(
    session: AsyncSession,
    *,
    recipient: Recipient,
    template_key: str,
    variables: Mapping[str, Any],
    dedupe_key: str,
    channels: Sequence[str] | None = None,
    urgency: Urgency = Urgency.NORMAL,
    approval_id: uuid.UUID | None = None,
    registry: RoleRegistry | None = None,
    extras: Extras | None = None,
    workflow: str | None = None,
    actor_type: ActorType = ActorType.SYSTEM,
    actor_id: str = NOTIFY_ACTOR,
    not_before: datetime | None = None,
) -> list[NotifyResult]:
    """Queue ``template_key`` for ``recipient`` on each channel. See the module docstring."""
    org_id = current_org(session)
    if org_id is None:
        raise BadRequestError("notify needs a tenant-bound session")
    if not dedupe_key or len(dedupe_key) > MAX_DEDUPE_KEY:
        raise BadRequestError("invalid dedupe key")
    extras = extras or Extras()
    if extras.link is not None and not is_app_path(extras.link):
        # The bell follows only a same-origin path of the app (``links.is_app_path``).
        raise BadRequestError("a notification link is an app path such as /inbox?item=<id>")
    if len(extras.attachments) > MAX_ATTACHMENTS:
        raise BadRequestError("too many attachments")
    wanted = list(channels) if channels is not None else list(CHANNELS)
    if any(c not in CHANNELS for c in wanted):
        raise BadRequestError("unknown channel")
    if recipient.user_id is None:
        wanted = [c for c in wanted if c == "email"]
    now = await db_now(session)
    # A portal action (a candidate's reply, a vendor's request) can queue a message;
    # queueing is the engine's work, not the portal person's: it reads the recipient's
    # preferences and address and the dedupe rows, which the portal guard hides
    # (migration 0022). Nothing of it goes back to the portal caller but the result.
    async with outside_portal(session):
        return await _notify_all(
            session,
            org_id=org_id,
            recipient=recipient,
            wanted=wanted,
            template_key=template_key,
            variables=variables,
            dedupe_key=dedupe_key,
            urgency=urgency,
            approval_id=approval_id,
            registry=registry,
            extras=extras,
            workflow=workflow,
            now=now,
            actor_type=actor_type,
            actor_id=actor_id,
            not_before=not_before,
        )


async def _notify_all(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    recipient: Recipient,
    wanted: list[str],
    template_key: str,
    variables: Mapping[str, Any],
    dedupe_key: str,
    urgency: Urgency,
    approval_id: uuid.UUID | None,
    registry: RoleRegistry | None,
    extras: Extras,
    workflow: str | None,
    now: datetime,
    actor_type: ActorType,
    actor_id: str,
    not_before: datetime | None,
) -> list[NotifyResult]:
    prefs = await _preferences(session, recipient.user_id) if recipient.user_id else None
    results: list[NotifyResult] = []
    for channel in wanted:
        results.append(
            await _notify_channel(
                session,
                org_id=org_id,
                recipient=recipient,
                prefs=prefs,
                channel=channel,
                template_key=template_key,
                variables=variables,
                dedupe_key=f"{dedupe_key}:{channel}",
                urgency=urgency,
                approval_id=approval_id,
                registry=registry,
                extras=extras,
                workflow=workflow,
                now=now,
                actor_type=actor_type,
                actor_id=actor_id,
                single_channel=len(wanted) == 1,
                not_before=not_before,
            )
        )
    return results


async def _notify_channel(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    recipient: Recipient,
    prefs: Preferences | None,
    channel: str,
    template_key: str,
    variables: Mapping[str, Any],
    dedupe_key: str,
    urgency: Urgency,
    approval_id: uuid.UUID | None,
    registry: RoleRegistry | None,
    extras: Extras,
    workflow: str | None,
    now: datetime,
    actor_type: ActorType,
    actor_id: str,
    single_channel: bool,
    not_before: datetime | None,
) -> NotifyResult:
    existing = await session.scalar(
        select(Notification).where(Notification.dedupe_key == dedupe_key)
    )
    if existing is not None:
        return NotifyResult(channel, existing.status, existing.reason, existing.id, replayed=True)

    locale = prefs.locale if prefs is not None else recipient.locale
    tz = prefs.timezone if prefs is not None else recipient.timezone
    found = await _render(
        session,
        template_key=template_key,
        channel=channel,
        locale=locale,
        variables=variables,
        tz=tz,
    )
    if found is None:
        log.info("notification_no_template", template_key=template_key, channel=channel)
        return NotifyResult(channel, "skipped", "no_template", None)
    template, rendered = found

    address: str | None = None
    missing_reason: str | None = None
    if channel == "email":
        if recipient.user_id is None:
            address = recipient.email
        else:
            address = await session.scalar(select(User.email).where(User.id == recipient.user_id))
        if not address:
            missing_reason = "no_address"
    elif channel == "slack" and recipient.user_id is not None:
        integration = await active_slack(session)
        link = (
            await slack_link_for_user(session, recipient.user_id, integration.external_id)
            if integration is not None
            else None
        )
        if link is None:
            missing_reason = "slack_not_linked"
        else:
            address = link.slack_user_id

    payload = message_payload(
        template=template,
        channel=channel,
        recipient=recipient_ref(recipient, address if channel == "email" else None),
        rendered=rendered,
    )
    digest = message_hash(payload)

    if template.requires_approval:
        if not single_channel:
            raise SensitiveTemplateError("one_channel_per_approval")
        try:
            await _check_sensitive(session, registry, approval_id, digest, template.key)
        except SensitiveTemplateError as exc:
            # The caller's transaction rolls back; callers record the refusal in a fresh
            # transaction with ``record_sensitive_refusal`` (the executor's pattern).
            log.warning(
                "sensitive_notification_refused", template_key=template.key, reason=exc.reason
            )
            raise
    elif approval_id is not None:
        raise BadRequestError("approval_id is only for templates that require approval")

    policy = category_policy(template.category)
    is_urgent = urgency is Urgency.URGENT or policy.always_urgent
    if missing_reason is not None:
        action, reason, deliver_at = "suppress", missing_reason, None
    elif prefs is None:
        action, reason, deliver_at = "send", "transactional", now
    else:
        decided = plan(
            prefs, category=template.category, channel=channel, urgent=is_urgent, now=now
        )
        action, reason, deliver_at = decided.action, decided.reason, decided.deliver_at

    if not_before is not None and action == "send" and channel != "in_app" and not_before > now:
        action, reason, deliver_at = "defer", "scheduled", not_before
    row_id = uuid.uuid4()
    content: dict[str, Any] | None = None
    if action != "suppress":
        title = rendered.subject or rendered.text.split("\n", 1)[0][:200]
        content = {
            "subject": rendered.subject,
            "text": rendered.text,
            "html": rendered.html,
            "title": title,
            "link": extras.link,
            "digest": {"title": title, "url": extras.link} if extras.link else {"title": title},
        }
        if channel == "slack":
            content["blocks"] = message_blocks(
                rendered.text, extras.slack_buttons, note=extras.slack_note
            )
        if channel == "email":
            content["auto_submitted"] = extras.auto_submitted
            content["in_reply_to"] = extras.in_reply_to
            content["list_unsubscribe"] = not policy.transactional and recipient.user_id is not None
            if extras.attachments:
                content["attachments"] = [a.to_json() for a in extras.attachments]
    status = {
        "send": NotificationStatus.SENT if channel == "in_app" else NotificationStatus.QUEUED,
        "defer": NotificationStatus.QUEUED,
        "digest": NotificationStatus.DIGEST,
        "suppress": NotificationStatus.SUPPRESSED,
    }[action]
    row = Notification(
        id=row_id,
        org_id=org_id,
        recipient_user_id=recipient.user_id,
        recipient_address=seal(
            Notification, "recipient_address", address, org_id=org_id, row_id=row_id
        )
        if address
        else None,
        recipient_bidx=blind_index("email", address, org_id)
        if channel == "email" and address and recipient.user_id is None
        else None,
        channel=channel,
        category=template.category,
        urgency=Urgency.URGENT.value if is_urgent else Urgency.NORMAL.value,
        template_key=template.key,
        template_id=template.id,
        locale=rendered.locale,
        status=status.value,
        reason=None if action == "send" else reason,
        content=seal(Notification, "content", content, org_id=org_id, row_id=row_id)
        if content is not None
        else None,
        content_hash=digest,
        dedupe_key=dedupe_key,
        approval_id=approval_id if template.requires_approval else None,
        workflow=workflow,
        attempts=0,
        next_attempt_at=deliver_at if status is NotificationStatus.QUEUED else None,
        sent_at=now if status is NotificationStatus.SENT else None,
    )
    session.add(row)
    await session.flush()
    action_name = {
        NotificationStatus.QUEUED: NotificationActions.QUEUED,
        NotificationStatus.SENT: NotificationActions.SENT,
        NotificationStatus.DIGEST: NotificationActions.HELD,
        NotificationStatus.SUPPRESSED: NotificationActions.SUPPRESSED,
    }[status]
    await audit_notification(session, action_name, row, actor_type=actor_type, actor_id=actor_id)
    return NotifyResult(channel, row.status, row.reason, row.id)
