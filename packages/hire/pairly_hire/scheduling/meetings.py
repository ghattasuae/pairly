# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""The organisation's meeting-link app (Zoom), combined with each organiser's calendar.

Zoom is used for an interview's link when both hold: the organisation turned on
``hire.scheduling.zoom`` and an administrator saved a working Server-to-Server OAuth
app (``save_zoom``: the account id, client id and secret, and optionally the Zoom user
that hosts every meeting; all encrypted in ``hire_meeting_integrations``, and checked by
asking Zoom for a token before they are kept). Then the meeting is created through Zoom
and the organiser's calendar provider (fake, Google or Microsoft) writes the event with
the Zoom link as its location instead of a Meet or Teams link. A link a person typed
(``manual``) always wins.

Nothing here runs without the flag and the saved app, and tests answer Zoom with a
mocked transport (``providers.set_google_transport``). Changes are audited
(``hire.meetings.zoom_saved``, ``hire.meetings.zoom_removed``) without the secret.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from pairly_core.audit import audit_actor_action
from pairly_core.crypto.types import seal
from pairly_core.errors import BadRequestError, ServiceUnavailableError
from pairly_core.integrations.calendar.base import CalendarError
from pairly_core.integrations.calendar.zoom import (
    ZoomCredentials,
    ZoomMeetingProvider,
    ZoomTokens,
)
from pairly_core.rbac import Actor
from pairly_hire.scheduling.models import HireMeetingIntegration
from pairly_hire.scheduling.providers import ZOOM_FLAG, flag_on
from pairly_hire.sealing import read_sealed

WORKFLOW: Final = "hire"
ENTITY: Final = "hire.meeting_integration"
_FIELD: Final = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_HOST: Final = re.compile(r"^[A-Za-z0-9._+@-]{1,254}$")


def _sealed(row: HireMeetingIntegration, column: str, value: str) -> Any:
    return seal(HireMeetingIntegration, column, value, org_id=row.org_id, row_id=row.id)


@dataclass(frozen=True, slots=True)
class ZoomStatus:
    flag_enabled: bool
    configured: bool
    status: str | None
    verified_at: datetime | None
    last_error: str | None

    @property
    def ready(self) -> bool:
        return self.flag_enabled and self.configured


async def _row(session: AsyncSession, *, lock: bool = False) -> HireMeetingIntegration | None:
    query = select(HireMeetingIntegration).where(HireMeetingIntegration.provider == "zoom")
    if lock:
        query = query.with_for_update()
    return await session.scalar(query)


async def zoom_status(session: AsyncSession) -> ZoomStatus:
    row = await _row(session)
    return ZoomStatus(
        flag_enabled=await flag_on(session, ZOOM_FLAG),
        configured=row is not None and row.status == "active",
        status=row.status if row else None,
        verified_at=row.verified_at if row else None,
        last_error=row.last_error if row else None,
    )


def _credentials(row: HireMeetingIntegration) -> ZoomCredentials | None:
    values = [read_sealed(row, c) for c in ("account_id", "client_id", "client_secret")]
    if not all(isinstance(v, str) and v for v in values):
        return None
    host = read_sealed(row, "host")
    return ZoomCredentials(values[0], values[1], values[2], host if isinstance(host, str) else "")


async def zoom_credentials(session: AsyncSession) -> ZoomCredentials | None:
    """The saved Zoom app when Zoom is in use for this organisation, else None."""
    if not await flag_on(session, ZOOM_FLAG):
        return None
    row = await _row(session)
    if row is None or row.status != "active":
        return None
    return _credentials(row)


def zoom_provider(client: httpx.AsyncClient, credentials: ZoomCredentials) -> ZoomMeetingProvider:
    return ZoomMeetingProvider(client, credentials)


def _check(value: str, name: str, pattern: re.Pattern[str] = _FIELD) -> str:
    value = value.strip()
    if not pattern.match(value):
        raise BadRequestError(f"the Zoom {name} is not valid")
    return value


async def save_zoom(
    session: AsyncSession,
    actor: Actor,
    client: httpx.AsyncClient,
    *,
    account_id: str,
    client_id: str,
    client_secret: str,
    host: str | None,
) -> ZoomStatus:
    """Check the app with Zoom (a token), then store it encrypted. Audited without the
    secret. A refused app is not stored (400); Zoom unreachable is 503."""
    credentials = ZoomCredentials(
        _check(account_id, "account id"),
        _check(client_id, "client id"),
        _check(client_secret, "client secret"),
        _check(host, "host user", _HOST) if host and host.strip() else "",
    )
    try:
        await ZoomTokens(credentials, client).access_token()
    except CalendarError as exc:
        if exc.transient:
            raise ServiceUnavailableError("Zoom could not be reached; try again") from None
        raise BadRequestError("Zoom refused these credentials; check the app") from None
    row = await _row(session, lock=True)
    now = datetime.now(UTC)
    if row is None:
        row = HireMeetingIntegration(id=uuid.uuid4(), org_id=actor.org_id, provider="zoom")
        session.add(row)
    row.account_id = _sealed(row, "account_id", credentials.account_id)
    row.client_id = _sealed(row, "client_id", credentials.client_id)
    row.client_secret = _sealed(row, "client_secret", credentials.client_secret)
    row.host = _sealed(row, "host", credentials.host) if credentials.host else None
    row.status = "active"
    row.last_error = None
    row.updated_by = actor.id
    row.verified_at = now
    row.updated_at = now
    await session.flush()
    await audit_actor_action(
        session,
        actor,
        "hire.meetings.zoom_saved",
        entity_type=ENTITY,
        entity_id=row.id,
        workflow=WORKFLOW,
        payload={"provider": "zoom", "host_set": bool(credentials.host)},
    )
    return await zoom_status(session)


async def remove_zoom(session: AsyncSession, actor: Actor) -> bool:
    """Turn the app off; the secret is overwritten, so nothing usable stays behind."""
    row = await _row(session, lock=True)
    if row is None or row.status == "revoked":
        return False
    row.status = "revoked"
    row.client_secret = _sealed(row, "client_secret", "revoked")
    row.updated_by = actor.id
    row.updated_at = datetime.now(UTC)
    await session.flush()
    await audit_actor_action(
        session,
        actor,
        "hire.meetings.zoom_removed",
        entity_type=ENTITY,
        entity_id=row.id,
        workflow=WORKFLOW,
        payload={"provider": "zoom"},
    )
    return True
