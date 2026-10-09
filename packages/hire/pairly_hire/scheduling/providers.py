# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Which calendar each person uses, and their Google connection.

A person uses Google Calendar when all three hold: the organisation turned on
``hire.scheduling.google``, the deployment has a Google OAuth client
(``load_google_oauth_config``), and the person connected their calendar (an active
``hire_calendar_connections`` row). Everyone else uses the fake calendar
(``pairly_core.integrations.calendar.fake``), stored in ``hire_calendar_blocks`` and
seeded per organisation. No external account is ever created here.

``CalendarSet`` answers busy time for a panel (each person through their own provider)
and gives the organiser's provider for writing events. A provider that fails raises
``CalendarError``: callers fail closed (no slot is offered or held on a calendar we could
not read).

The Google HTTP client uses ``set_google_transport`` in tests (a mocked transport);
in production it is the default transport, with a timeout.
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
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from pairly_core.audit import audit_actor_action
from pairly_core.crypto.types import seal
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
FEATURE_FLAGS: Final[tuple[FlagSpec, ...]] = (
    FlagSpec(
        GOOGLE_FLAG,
        "Use Google Calendar and Google Meet for interviews (needs a Google OAuth client)",
        default=False,
    ),
)

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


async def google_enabled(session: AsyncSession) -> bool:
    """The organisation's flag; an unregistered flag reads as off (fail closed)."""
    try:
        return await is_enabled(session, GOOGLE_FLAG)
    except FlagError:
        return False


@dataclass(frozen=True, slots=True)
class Person:
    """A panelist or organiser as the calendars see them."""

    id: uuid.UUID
    email: str
    timezone: str


class ConnectionTokens:
    """``TokenSource`` for one Google connection: the refresh token, decrypted only when
    an access token is needed, exchanged once per instance."""

    def __init__(self, oauth: GoogleOAuth, connection: HireCalendarConnection) -> None:
        self.oauth = oauth
        self.connection = connection
        self._token: str | None = None
        self._until: datetime | None = None

    async def access_token(self) -> str:
        now = datetime.now(UTC)
        if self._token is not None and self._until is not None and now < self._until:
            return self._token
        refresh = read_sealed(self.connection, "refresh_token")
        if not isinstance(refresh, str) or not refresh:
            raise ServiceUnavailableError("the calendar connection is broken; reconnect it")
        tokens = await self.oauth.refresh(refresh)
        self._token = tokens.access_token
        self._until = now + timedelta(seconds=max(tokens.expires_in - 60, 30))
        return self._token


@dataclass
class CalendarSet:
    """Each person's provider for one request (see the module docstring)."""

    fake: FakeCalendarProvider
    google: GoogleCalendarProvider | None = None
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
    """The calendars of ``people`` for one request (closes the Google client after)."""
    fake = FakeCalendarProvider(DbFakeCalendarStore(session, org_id), seed=seed)
    config = google_config()
    if config is None or not people or not await google_enabled(session):
        yield CalendarSet(fake)
        return
    connections = {
        c.user_id: c
        for c in await session.scalars(
            select(HireCalendarConnection).where(
                HireCalendarConnection.provider == "google",
                HireCalendarConnection.status == "active",
                HireCalendarConnection.user_id.in_([p.id for p in people]),
            )
        )
    }
    if not connections:
        yield CalendarSet(fake)
        return
    async with google_client() as client:
        oauth = GoogleOAuth(config, client)
        google = GoogleCalendarProvider(client)
        found = CalendarSet(fake, google)
        for person in people:
            connection = connections.get(person.id)
            if connection is None:
                continue
            found.users[person.id] = CalendarUser(
                str(person.id),
                person.email,
                person.timezone,
                tokens=ConnectionTokens(oauth, connection),
            )
            found.providers[person.id] = google
        yield found


# ------------------------------------------------------------------ connections


@dataclass(frozen=True, slots=True)
class ConnectionView:
    provider: str
    status: str
    connected_at: datetime | None
    account_email: str | None


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
) -> None:
    """Store (or replace) the actor's Google connection, the token encrypted. Audited
    without the token."""
    if not refresh_token:
        raise ConflictError("Google did not return a refresh token; connect again")
    row = await session.scalar(
        select(HireCalendarConnection)
        .where(
            HireCalendarConnection.user_id == actor.id,
            HireCalendarConnection.provider == "google",
        )
        .with_for_update()
    )
    now = datetime.now(UTC)
    if row is None:
        row = HireCalendarConnection(
            id=uuid.uuid4(), org_id=actor.org_id, user_id=actor.id, provider="google"
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
        payload={"provider": "google", "scopes": row.scopes},
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
