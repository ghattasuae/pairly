# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Which calendar each person uses, and their Google or Microsoft connection.

A person uses Google Calendar when all three hold: the organisation turned on
``hire.scheduling.google``, the deployment has a Google OAuth client
(``load_google_oauth_config``), and the person connected their calendar (an active
``hire_calendar_connections`` row). Microsoft 365 is the same with
``hire.scheduling.microsoft`` and ``load_microsoft_oauth_config``. A person with both
uses Google. Everyone else uses the fake calendar
(``pairly_core.integrations.calendar.fake``), stored in ``hire_calendar_blocks`` and
seeded per organisation. No external account is ever created here.

``CalendarSet`` answers busy time for a panel (each person through their own provider)
and gives the organiser's provider for writing events. A provider that fails raises
``CalendarError``: callers fail closed (no slot is offered or held on a calendar we could
not read).

The HTTP client of the external providers uses ``set_google_transport`` in tests (a
mocked transport answering Google, Microsoft and Zoom alike); in production it is the
default transport, with a timeout.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Final

import httpx
import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from pairly_core.audit import ActorType, append_audit, audit_actor_action
from pairly_core.crypto.types import seal
from pairly_core.db import create_sessionmaker, set_tenant
from pairly_core.errors import ConflictError, ServiceUnavailableError
from pairly_core.flags.registry import FlagError, FlagSpec
from pairly_core.flags.service import is_enabled
from pairly_core.integrations.calendar.base import (
    CalendarProvider,
    CalendarUser,
)
from pairly_core.integrations.calendar.fake import FakeCalendarProvider
from pairly_core.integrations.calendar.google import (
    GoogleCalendarProvider,
    GoogleOAuth,
    GoogleOAuthConfig,
    load_google_oauth_config,
)
from pairly_core.integrations.calendar.microsoft import (
    MicrosoftGraphCalendarProvider,
    MicrosoftOAuth,
    MicrosoftOAuthConfig,
    load_microsoft_oauth_config,
)
from pairly_core.rbac import Actor
from pairly_core.scheduling.availability import Interval
from pairly_hire.scheduling.fake_store import DbFakeCalendarStore
from pairly_hire.scheduling.models import HireCalendarConnection
from pairly_hire.sealing import read_sealed


def _sealed(model: Any, column: str, value: Any, *, org_id: uuid.UUID, row_id: object) -> Any:
    """``seal`` typed as ``Any``: an encrypted column takes the plaintext wrapper."""
    return seal(model, column, value, org_id=org_id, row_id=row_id)


log = structlog.get_logger("pairly_hire.scheduling.providers")

WORKFLOW: Final = "hire"
GOOGLE_FLAG: Final = "hire.scheduling.google"
MICROSOFT_FLAG: Final = "hire.scheduling.microsoft"
ZOOM_FLAG: Final = "hire.scheduling.zoom"
FEATURE_FLAGS: Final[tuple[FlagSpec, ...]] = (
    FlagSpec(
        GOOGLE_FLAG,
        "Use Google Calendar and Google Meet for interviews (needs a Google OAuth client)",
        default=False,
    ),
    FlagSpec(
        MICROSOFT_FLAG,
        "Use Microsoft 365 calendars and Teams for interviews (needs a Microsoft Entra app)",
        default=False,
    ),
    FlagSpec(
        ZOOM_FLAG,
        "Use Zoom meeting links for interviews (needs the organisation's Zoom app)",
        default=False,
    ),
)
# The calendar providers a person can connect, in order of preference.
CALENDAR_PROVIDERS: Final = ("google", "microsoft")

_google_transport: httpx.AsyncBaseTransport | None = None


def set_google_transport(transport: httpx.AsyncBaseTransport | None) -> None:
    """Tests: answer Google calls with a mocked transport (None: back to the network)."""
    global _google_transport
    _google_transport = transport


@asynccontextmanager
async def google_client() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=_google_transport, timeout=15.0, follow_redirects=False
    ) as client:
        yield client


def google_config() -> GoogleOAuthConfig | None:
    return load_google_oauth_config()


def microsoft_config() -> MicrosoftOAuthConfig | None:
    return load_microsoft_oauth_config()


async def flag_on(session: AsyncSession, key: str) -> bool:
    """The organisation's flag; an unregistered flag reads as off (fail closed)."""
    try:
        return await is_enabled(session, key)
    except FlagError:
        return False


async def google_enabled(session: AsyncSession) -> bool:
    return await flag_on(session, GOOGLE_FLAG)


async def microsoft_enabled(session: AsyncSession) -> bool:
    return await flag_on(session, MICROSOFT_FLAG)


async def ready_calendar_providers(session: AsyncSession) -> dict[str, Any]:
    """The external calendar providers this organisation and deployment can use, with
    the OAuth client of each (flag on and client configured)."""
    out: dict[str, Any] = {}
    google = google_config()
    if google is not None and await google_enabled(session):
        out["google"] = google
    microsoft = microsoft_config()
    if microsoft is not None and await microsoft_enabled(session):
        out["microsoft"] = microsoft
    return out


def oauth_for(provider: str, config: Any, client: httpx.AsyncClient) -> Any:
    """The OAuth helper of ``provider`` (``GoogleOAuth`` or ``MicrosoftOAuth``)."""
    if provider == "microsoft":
        return MicrosoftOAuth(config, client)
    return GoogleOAuth(config, client)


@dataclass(frozen=True, slots=True)
class Person:
    """A panelist or organiser as the calendars see them."""

    id: uuid.UUID
    email: str
    timezone: str


ROTATED_ACTION: Final = "hire.calendar.token_rotated"
ROTATION_ACTOR: Final = "pairly.hire.calendar"


def _side_session(session: AsyncSession, org_id: uuid.UUID) -> AsyncSession:
    """A second session on the same database, outside the caller's transaction."""
    bind = session.bind
    if not isinstance(bind, AsyncEngine):  # pragma: no cover - sessions bind an engine
        raise RuntimeError("storing a rotated token needs a session bound to an engine")
    side = create_sessionmaker(bind)()
    set_tenant(side, org_id, None)
    return side


async def store_rotated_token(
    session: AsyncSession, connection: HireCalendarConnection, refresh_token: str
) -> bool:
    """Store the refresh token the provider rotated to, in its own short transaction (so
    it survives whatever the caller's transaction does: the old token may already be
    dead). Only an active connection is updated (a disconnect in between wins). Audited
    without the token. Returns whether it was stored; a failure is logged, never raised
    (the access token in hand still works)."""
    try:
        side = _side_session(session, connection.org_id)
        async with side, side.begin():
            row = await side.scalar(
                select(HireCalendarConnection)
                .where(
                    HireCalendarConnection.id == connection.id,
                    HireCalendarConnection.status == "active",
                )
                .with_for_update()
            )
            if row is None:
                return False
            row.refresh_token = _sealed(
                HireCalendarConnection,
                "refresh_token",
                refresh_token,
                org_id=row.org_id,
                row_id=row.id,
            )
            row.updated_at = datetime.now(UTC)
            await side.flush()
            await append_audit(
                side,
                action=ROTATED_ACTION,
                actor_type=ActorType.SYSTEM,
                actor_id=ROTATION_ACTOR,
                entity_type="hire.calendar_connection",
                entity_id=row.id,
                workflow=WORKFLOW,
                payload={"provider": row.provider, "user_id": str(row.user_id)},
                org_id=row.org_id,
            )
    except Exception:  # never break the calendar call over this
        log.warning("calendar_token_rotation_not_stored", connection_id=str(connection.id))
        return False
    return True


class ConnectionTokens:
    """``TokenSource`` for one Google or Microsoft connection: the refresh token,
    decrypted only when an access token is needed, exchanged once per instance.

    Microsoft (and Google, when it does) may return a new refresh token on refresh. It
    is stored at once through ``store_rotated_token`` (its own short transaction,
    audited without the token), so a connection keeps working past the first token's
    lifetime (90 days at Microsoft) and nobody has to reconnect."""

    def __init__(
        self,
        oauth: GoogleOAuth | MicrosoftOAuth,
        connection: HireCalendarConnection,
        session: AsyncSession | None = None,
    ) -> None:
        self.oauth = oauth
        self.connection = connection
        self.session = session
        self._refresh: str | None = None
        self._token: str | None = None
        self._until: datetime | None = None

    async def access_token(self) -> str:
        now = datetime.now(UTC)
        if self._token is not None and self._until is not None and now < self._until:
            return self._token
        refresh = self._refresh or read_sealed(self.connection, "refresh_token")
        if not isinstance(refresh, str) or not refresh:
            raise ServiceUnavailableError("the calendar connection is broken; reconnect it")
        tokens = await self.oauth.refresh(refresh)
        rotated = tokens.refresh_token
        if rotated and rotated != refresh and self.session is not None:
            await store_rotated_token(self.session, self.connection, rotated)
            # Later refreshes by this instance use the new token (kept in memory; the
            # caller's row is not touched, a read must not write in its transaction).
            self._refresh = rotated
        self._token = tokens.access_token
        self._until = now + timedelta(seconds=max(tokens.expires_in - 60, 30))
        return self._token


@dataclass
class CalendarSet:
    """Each person's provider for one request (see the module docstring)."""

    fake: FakeCalendarProvider
    google: GoogleCalendarProvider | None = None
    microsoft: MicrosoftGraphCalendarProvider | None = None
    users: dict[uuid.UUID, CalendarUser] = field(default_factory=dict)
    providers: dict[uuid.UUID, CalendarProvider] = field(default_factory=dict)

    def user(self, person: Person) -> CalendarUser:
        return self.users.get(person.id) or CalendarUser(
            str(person.id), person.email, person.timezone
        )

    def provider_for(self, person_id: uuid.UUID) -> CalendarProvider:
        return self.providers.get(person_id, self.fake)

    async def busy(
        self, people: Sequence[Person], window: Interval
    ) -> Mapping[uuid.UUID, list[Interval]]:
        groups: dict[int, tuple[CalendarProvider, list[CalendarUser]]] = {}
        for person in people:
            provider = self.provider_for(person.id)
            groups.setdefault(id(provider), (provider, []))[1].append(self.user(person))
        out: dict[uuid.UUID, list[Interval]] = {}
        for provider, users in groups.values():
            found = await provider.free_busy(users, window)
            for user in users:
                out[uuid.UUID(user.key)] = list(found.get(user.key, []))
        return out


@asynccontextmanager
async def calendar_set(
    session: AsyncSession, org_id: uuid.UUID, seed: str, people: Sequence[Person]
) -> AsyncIterator[CalendarSet]:
    """The calendars of ``people`` for one request (closes the HTTP client after)."""
    fake = FakeCalendarProvider(DbFakeCalendarStore(session, org_id), seed=seed)
    ready = await ready_calendar_providers(session) if people else {}
    if not ready:
        yield CalendarSet(fake)
        return
    rows = await session.scalars(
        select(HireCalendarConnection).where(
            HireCalendarConnection.provider.in_(list(ready)),
            HireCalendarConnection.status == "active",
            HireCalendarConnection.user_id.in_([p.id for p in people]),
        )
    )
    connections: dict[uuid.UUID, HireCalendarConnection] = {}
    for row in sorted(rows, key=lambda r: CALENDAR_PROVIDERS.index(r.provider)):
        connections.setdefault(row.user_id, row)
    if not connections:
        yield CalendarSet(fake)
        return
    async with google_client() as client:
        found = CalendarSet(
            fake,
            GoogleCalendarProvider(client) if "google" in ready else None,
            MicrosoftGraphCalendarProvider(client) if "microsoft" in ready else None,
        )
        oauth = {name: oauth_for(name, config, client) for name, config in ready.items()}
        for person in people:
            connection = connections.get(person.id)
            if connection is None:
                continue
            provider: CalendarProvider | None = (
                found.google if connection.provider == "google" else found.microsoft
            )
            if provider is None:  # pragma: no cover - filtered by the query
                continue
            found.users[person.id] = CalendarUser(
                str(person.id),
                person.email,
                person.timezone,
                tokens=ConnectionTokens(oauth[connection.provider], connection, session),
            )
            found.providers[person.id] = provider
        yield found


# ------------------------------------------------------------------ connections


@dataclass(frozen=True, slots=True)
class ConnectionView:
    provider: str
    status: str
    connected_at: datetime | None
    account_email: str | None


async def connection_counts(session: AsyncSession) -> dict[str, int]:
    """Active calendar connections per provider across the organisation."""
    rows = await session.execute(
        select(HireCalendarConnection.provider, func.count())
        .where(HireCalendarConnection.status == "active")
        .group_by(HireCalendarConnection.provider)
    )
    return {provider: int(count) for provider, count in rows.all()}


async def my_connections(session: AsyncSession, user_id: uuid.UUID) -> list[ConnectionView]:
    rows = await session.scalars(
        select(HireCalendarConnection).where(HireCalendarConnection.user_id == user_id)
    )
    out: list[ConnectionView] = []
    for row in rows:
        email = read_sealed(row, "account_email")
        out.append(
            ConnectionView(
                row.provider,
                row.status,
                row.connected_at,
                email if isinstance(email, str) else None,
            )
        )
    return out


async def save_google_connection(
    session: AsyncSession,
    actor: Actor,
    *,
    refresh_token: str,
    scopes: str,
    account_email: str | None,
    provider: str = "google",
) -> None:
    """Store (or replace) the actor's Google or Microsoft connection, the token
    encrypted. Audited without the token."""
    if provider not in CALENDAR_PROVIDERS:
        raise ConflictError("unknown calendar provider")
    if not refresh_token:
        raise ConflictError("the calendar did not return a refresh token; connect again")
    row = await session.scalar(
        select(HireCalendarConnection)
        .where(
            HireCalendarConnection.user_id == actor.id,
            HireCalendarConnection.provider == provider,
        )
        .with_for_update()
    )
    now = datetime.now(UTC)
    if row is None:
        row = HireCalendarConnection(
            id=uuid.uuid4(), org_id=actor.org_id, user_id=actor.id, provider=provider
        )
        session.add(row)
    row.refresh_token = _sealed(
        HireCalendarConnection, "refresh_token", refresh_token, org_id=actor.org_id, row_id=row.id
    )
    row.account_email = (
        _sealed(
            HireCalendarConnection,
            "account_email",
            account_email,
            org_id=actor.org_id,
            row_id=row.id,
        )
        if account_email
        else None
    )
    row.scopes = scopes[:500]
    row.status = "active"
    row.last_error = None
    row.connected_at = now
    row.updated_at = now
    await session.flush()
    await audit_actor_action(
        session,
        actor,
        "hire.calendar.connected",
        entity_type="hire.calendar_connection",
        entity_id=row.id,
        workflow=WORKFLOW,
        payload={"provider": provider, "scopes": row.scopes},
    )


async def disconnect(session: AsyncSession, actor: Actor, provider: str) -> bool:
    row = await session.scalar(
        select(HireCalendarConnection)
        .where(
            HireCalendarConnection.user_id == actor.id,
            HireCalendarConnection.provider == provider,
        )
        .with_for_update()
    )
    if row is None or row.status == "revoked":
        return False
    row.status = "revoked"
    # The token is replaced, so nothing usable stays behind.
    row.refresh_token = _sealed(
        HireCalendarConnection, "refresh_token", "revoked", org_id=row.org_id, row_id=row.id
    )
    row.updated_at = datetime.now(UTC)
    await session.flush()
    await audit_actor_action(
        session,
        actor,
        "hire.calendar.disconnected",
        entity_type="hire.calendar_connection",
        entity_id=row.id,
        workflow=WORKFLOW,
        payload={"provider": provider},
    )
    return True
