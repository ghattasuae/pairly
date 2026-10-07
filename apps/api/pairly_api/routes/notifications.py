# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""In-app notifications, live updates, preferences and one-click unsubscribe (WP1.9).

* ``GET  /v1/notifications``                  the caller's in-app items, newest first
* ``POST /v1/notifications/{id}/read``        mark one read
* ``POST /v1/notifications/read-all``         mark all read
* ``GET  /v1/notifications/stream``           server-sent events: new in-app items
* ``GET|PUT /v1/notifications/preferences``   the caller's preferences
* ``POST /v1/notifications/unsubscribe``      RFC 8058 one-click unsubscribe (no sign-in)

Everything except unsubscribe uses the same bearer JWT and ``X-Pairly-Org`` header as
the rest of the API, and only ever touches the signed-in user's own rows (RLS scopes to
the organisation, every query also filters on the user). The event stream is a normal
authenticated GET (the web app reads it with ``fetch``, so the token stays in the
``Authorization`` header and never in a URL). It polls the database, sends only rows
created after the stream started (or after ``Last-Event-ID``), re-checks the membership
on every poll, and closes when the token expires or after ``PAIRLY_SSE_MAX_SECONDS``;
the client reconnects with a fresh token.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select, update

from pairly_core.audit import ActorType, append_audit, audit_actor_action
from pairly_core.crypto.types import unseal
from pairly_core.db import set_tenant
from pairly_core.errors import BadRequestError, NotFoundError, ServiceUnavailableError
from pairly_core.identity import Context, CurrentActor, OrgSession, VerifiedSubject
from pairly_core.models import Membership
from pairly_core.notifications.models import Notification, NotificationPreference
from pairly_core.notifications.preferences import (
    CATEGORIES,
    PreferenceError,
    parse_preferences,
    preferences_from_row,
)
from pairly_core.notifications.service import NotificationActions
from pairly_core.notifications.settings import NotificationSettings
from pairly_core.notifications.unsubscribe import read_token

router = APIRouter(prefix="/notifications", tags=["notifications"])


def get_notification_settings(request: Request) -> NotificationSettings:
    settings = getattr(request.app.state, "notifications", None)
    if not isinstance(settings, NotificationSettings):
        raise ServiceUnavailableError("notifications are not configured")
    return settings


NotifySettings = Annotated[NotificationSettings, Depends(get_notification_settings)]


class NotificationOut(BaseModel):
    id: uuid.UUID
    category: str
    urgency: str
    title: str
    body: str
    link: str | None
    created_at: datetime
    read_at: datetime | None


class NotificationPage(BaseModel):
    items: list[NotificationOut]
    unread: int


def _out(row: Notification) -> NotificationOut:
    content: dict[str, Any] = unseal(row, "content") or {}
    return NotificationOut(
        id=row.id,
        category=row.category,
        urgency=row.urgency,
        title=str(content.get("title") or ""),
        body=str(content.get("text") or ""),
        link=content.get("link") if isinstance(content.get("link"), str) else None,
        created_at=row.created_at,
        read_at=row.read_at,
    )


def _mine(actor_id: uuid.UUID) -> Any:
    return (
        (Notification.recipient_user_id == actor_id)
        & (Notification.channel == "in_app")
        & (Notification.status == "sent")
    )


@router.get("", response_model=NotificationPage)
async def list_notifications(
    actor: CurrentActor,
    session: OrgSession,
    unread: bool = False,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    before: datetime | None = None,
) -> NotificationPage:
    query = select(Notification).where(_mine(actor.id))
    if unread:
        query = query.where(Notification.read_at.is_(None))
    if before is not None:
        query = query.where(Notification.created_at < before)
    rows = list(
        await session.scalars(
            query.order_by(Notification.created_at.desc(), Notification.id.desc()).limit(limit)
        )
    )
    count = await session.scalar(
        select(func.count())
        .select_from(Notification)
        .where(_mine(actor.id), Notification.read_at.is_(None))
    )
    return NotificationPage(items=[_out(r) for r in rows], unread=int(count or 0))


@router.post("/{notification_id}/read", response_model=NotificationOut)
async def mark_read(
    notification_id: uuid.UUID, actor: CurrentActor, session: OrgSession
) -> NotificationOut:
    row = await session.scalar(
        select(Notification).where(Notification.id == notification_id, _mine(actor.id))
    )
    if row is None:
        raise NotFoundError("notification not found")
    if row.read_at is None:
        row.read_at = datetime.now(row.created_at.tzinfo)
        await session.flush()
        await audit_actor_action(
            session,
            actor,
            NotificationActions.READ,
            entity_type="notification",
            entity_id=row.id,
            payload={"count": 1},
        )
    return _out(row)


@router.post("/read-all")
async def mark_all_read(actor: CurrentActor, session: OrgSession) -> dict[str, int]:
    result = await session.execute(
        update(Notification)
        .where(_mine(actor.id), Notification.read_at.is_(None))
        .values(read_at=datetime.now().astimezone())
        .execution_options(synchronize_session=False)
    )
    count = int(getattr(result, "rowcount", 0) or 0)
    if count:
        await audit_actor_action(
            session,
            actor,
            NotificationActions.READ,
            entity_type="user",
            entity_id=actor.id,
            payload={"count": count},
        )
    return {"marked": count}


# ------------------------------------------------------------------ server-sent events


def _sse(event: str, data: dict[str, Any], event_id: str | None = None) -> str:
    lines = []
    if event_id:
        lines.append(f"id: {event_id}")
    lines.append(f"event: {event}")
    lines.append("data: " + json.dumps(data, separators=(",", ":"), ensure_ascii=False))
    return "\n".join(lines) + "\n\n"


@router.get("/stream")
async def stream(
    request: Request,
    actor: CurrentActor,
    verified: VerifiedSubject,
    ctx: Context,
    settings: NotifySettings,
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID", max_length=64)] = None,
) -> StreamingResponse:
    org_id, user_id, subject = actor.org_id, actor.id, actor.subject
    sessionmaker = ctx.sessionmaker
    exp = verified.claims.get("exp")
    deadline = time.time() + settings.sse_max_seconds
    if isinstance(exp, int | float):
        deadline = min(deadline, float(exp))
    resume_from: uuid.UUID | None = None
    if last_event_id:
        try:
            resume_from = uuid.UUID(last_event_id)
        except ValueError:
            raise BadRequestError("invalid Last-Event-ID") from None

    async def events() -> AsyncIterator[str]:
        cursor: tuple[datetime, uuid.UUID] | None = None
        async with sessionmaker() as session:
            set_tenant(session, org_id, subject)
            async with session.begin():
                if resume_from is not None:
                    row = await session.scalar(
                        select(Notification).where(Notification.id == resume_from, _mine(user_id))
                    )
                    if row is not None:
                        cursor = (row.created_at, row.id)
                if cursor is None:
                    newest = await session.scalar(
                        select(Notification)
                        .where(_mine(user_id))
                        .order_by(Notification.created_at.desc(), Notification.id.desc())
                        .limit(1)
                    )
                    cursor = (newest.created_at, newest.id) if newest is not None else None
        yield f"retry: {int(settings.sse_poll_seconds * 1000)}\n\n"
        while time.time() < deadline:
            if await request.is_disconnected():
                return
            async with sessionmaker() as session:
                set_tenant(session, org_id, subject)
                async with session.begin():
                    member = await session.scalar(
                        select(Membership.id).where(Membership.user_id == user_id).limit(1)
                    )
                    if member is None:
                        yield _sse("end", {"reason": "access_revoked"})
                        return
                    query = select(Notification).where(_mine(user_id))
                    if cursor is not None:
                        query = query.where(
                            or_(
                                Notification.created_at > cursor[0],
                                (Notification.created_at == cursor[0])
                                & (Notification.id > cursor[1]),
                            )
                        )
                    rows = list(
                        await session.scalars(
                            query.order_by(Notification.created_at, Notification.id).limit(50)
                        )
                    )
                    for row in rows:
                        item = _out(row)
                        cursor = (row.created_at, row.id)
                        yield _sse("notification", item.model_dump(mode="json"), str(row.id))
            yield ": keepalive\n\n"
            await asyncio.sleep(settings.sse_poll_seconds)
        yield _sse("end", {"reason": "reconnect"})

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


# ------------------------------------------------------------------ preferences


class PreferencesIn(BaseModel):
    locale: Literal["en", "ar"] = "en"
    timezone: str = Field(default="UTC", max_length=64)
    channels: dict[str, list[Literal["email", "slack", "in_app"]]] = Field(default_factory=dict)
    digest: dict[str, Literal["realtime", "daily"]] = Field(default_factory=dict)
    snooze_until: datetime | None = None
    quiet_start: Annotated[int | None, Field(ge=0, le=1439)] = None
    quiet_end: Annotated[int | None, Field(ge=0, le=1439)] = None


class PreferencesOut(BaseModel):
    locale: str
    timezone: str
    channels: dict[str, list[str]]
    digest: dict[str, str]
    snooze_until: datetime | None
    quiet_start: int | None
    quiet_end: int | None
    categories: list[str]


def _prefs_out(row: NotificationPreference | None) -> PreferencesOut:
    prefs = preferences_from_row(row)
    configurable = sorted(k for k, v in CATEGORIES.items() if v.configurable)
    return PreferencesOut(
        locale="ar" if prefs.locale == "ar" else "en",
        timezone=prefs.timezone,
        channels={c: sorted(prefs.enabled(c)) for c in configurable},
        digest={c: prefs.delivery(c) for c in configurable},
        snooze_until=prefs.snooze_until,
        quiet_start=prefs.quiet_start,
        quiet_end=prefs.quiet_end,
        categories=configurable,
    )


@router.get("/preferences", response_model=PreferencesOut)
async def get_preferences(actor: CurrentActor, session: OrgSession) -> PreferencesOut:
    row = await session.scalar(
        select(NotificationPreference).where(NotificationPreference.user_id == actor.id)
    )
    return _prefs_out(row)


@router.put("/preferences", response_model=PreferencesOut)
async def put_preferences(
    body: PreferencesIn, actor: CurrentActor, session: OrgSession
) -> PreferencesOut:
    try:
        prefs = parse_preferences(
            locale=body.locale,
            timezone=body.timezone,
            channels=body.channels,
            digest=body.digest,
            snooze_until=body.snooze_until,
            quiet_start=body.quiet_start,
            quiet_end=body.quiet_end,
        )
    except PreferenceError as exc:
        raise BadRequestError(str(exc)) from None
    row = await session.scalar(
        select(NotificationPreference)
        .where(NotificationPreference.user_id == actor.id)
        .with_for_update()
    )
    if row is None:
        row = NotificationPreference(id=uuid.uuid4(), org_id=actor.org_id, user_id=actor.id)
        session.add(row)
    row.locale = prefs.locale
    row.timezone = prefs.timezone
    row.channels = {k: sorted(v) for k, v in prefs.channels.items()}
    row.digest = dict(prefs.digest)
    row.snooze_until = prefs.snooze_until
    row.quiet_start = prefs.quiet_start
    row.quiet_end = prefs.quiet_end
    row.updated_at = datetime.now().astimezone()
    await session.flush()
    await audit_actor_action(
        session,
        actor,
        NotificationActions.PREFERENCES_CHANGED,
        entity_type="user",
        entity_id=actor.id,
        payload={
            "locale": prefs.locale,
            "channels": {k: sorted(v) for k, v in prefs.channels.items()},
            "digest": dict(prefs.digest),
            "snoozed": prefs.snooze_until is not None,
            "quiet_hours": prefs.quiet_start is not None,
        },
    )
    return _prefs_out(row)


# ------------------------------------------------------------------ unsubscribe


@router.post("/unsubscribe", status_code=204)
async def unsubscribe(
    ctx: Context,
    settings: NotifySettings,
    token: Annotated[str, Query(min_length=10, max_length=200)],
) -> Response:
    """RFC 8058 one-click unsubscribe: turns email off for one category of one person.

    No sign-in: the HMAC token is the proof. Invalid tokens get the same 204, so the
    endpoint reveals nothing. The token sits in the query string, which the access log
    leaves out.
    """
    secret = settings.signed_link_secret
    claim = read_token(secret.get_secret_value(), token) if secret is not None else None
    if (
        claim is None
        or claim.category not in CATEGORIES
        or not CATEGORIES[claim.category].configurable
    ):
        return Response(status_code=204)
    async with ctx.sessionmaker() as session:
        set_tenant(session, claim.org_id, None)
        async with session.begin():
            member = await session.scalar(
                select(Membership.id).where(Membership.user_id == claim.user_id).limit(1)
            )
            if member is None:
                return Response(status_code=204)
            row = await session.scalar(
                select(NotificationPreference)
                .where(NotificationPreference.user_id == claim.user_id)
                .with_for_update()
            )
            if row is None:
                row = NotificationPreference(
                    id=uuid.uuid4(),
                    org_id=claim.org_id,
                    user_id=claim.user_id,
                    locale="en",
                    timezone="UTC",
                    channels={},
                    digest={},
                )
                session.add(row)
            prefs = preferences_from_row(row)
            channels = dict(row.channels or {})
            channels[claim.category] = sorted(prefs.enabled(claim.category) - {"email"})
            row.channels = channels
            row.updated_at = datetime.now().astimezone()
            await session.flush()
            await append_audit(
                session,
                action=NotificationActions.UNSUBSCRIBED,
                actor_type=ActorType.USER,
                actor_id=str(claim.user_id),
                entity_type="user",
                entity_id=claim.user_id,
                payload={"category": claim.category, "channel": "email"},
            )
    return Response(status_code=204)
