# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Async SQLAlchemy setup and tenant isolation.

Tenant isolation has two layers:

1. Postgres Row Level Security (the real boundary). Every tenant table has RLS enabled
   and forced, with policies keyed on the transaction-local setting ``app.org_id``. The
   application connects as a non-owner role without BYPASSRLS. With no org set, policies
   match no rows, so a forgotten tenant context fails closed.
2. An ORM filter (defence in depth). Every ORM SELECT, UPDATE and DELETE touching a
   tenant model gets ``org_id = <current org>`` added, and flushes refuse to write a row
   for another org.

The tenant context lives in ``session.info`` and is re-applied with
``set_config('app.org_id', ..., true)`` at the start of every transaction, so it never
leaks to another request through the connection pool (the setting is transaction-local).
"""

from __future__ import annotations

import re
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from datetime import datetime
from typing import Any

from sqlalchemy import (
    DateTime,
    ForeignKey,
    MetaData,
    Uuid,
    event,
    false,
    func,
    or_,
    select,
    text,
)
from sqlalchemy.engine import Connection
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    ORMExecuteState,
    Session,
    SessionTransaction,
    mapped_column,
    with_loader_criteria,
)

from pairly_core.config import Settings

ORG_SETTING = "app.org_id"
SUBJECT_SETTING = "app.auth_subject"
# An external person signed in to a portal (a candidate or a vendor's contact), for
# example ``hire_candidate:<uuid>``. Empty for staff, agents and jobs. Workflows add
# restrictive Row Level Security policies keyed on it (``set_portal_subject``), and core
# tables are closed to it except narrow rules (migration 0022, ``outside_portal``).
PORTAL_SETTING = "app.portal_subject"

_INFO_ORG = "pairly.org_id"
_INFO_SUBJECT = "pairly.auth_subject"
_INFO_PORTAL = "pairly.portal_subject"
_PORTAL_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}:[0-9a-f-]{36}$")

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class TenantError(RuntimeError):
    """Raised when a write would cross or lack a tenant boundary."""


class Base(DeclarativeBase):
    """Declarative base for every Pairly table (core and workflow plugins)."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class Timestamped:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class OrgScoped:
    """Mixin for tenant tables: a non-null, indexed ``org_id``.

    Every table using it must also get the RLS policy in its migration (see
    ``pairly_core.migration_helpers.enable_org_rls``).
    """

    org_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("organisations.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )


class OrgScopedOrShared:
    """Mixin for tables mixing shared rows (``org_id`` NULL) and per-org rows (roles)."""

    org_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey("organisations.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )


class OrgRoot:
    """Primary key mixin for the organisations table: a tenant only sees its own row."""

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4, server_default=text("gen_random_uuid()")
    )


# ------------------------------------------------------------------ tenant context


def set_tenant(session: AsyncSession, org_id: uuid.UUID | None, auth_subject: str | None) -> None:
    """Set the tenant context for ``session``. Call before the transaction begins.

    The context applies from the next transaction start. Raises if a transaction is
    already open, because the RLS setting would otherwise be missing for it.
    """
    if session.in_transaction():
        raise TenantError("set the tenant context before the transaction begins")
    if org_id is not None and not isinstance(org_id, uuid.UUID):
        raise TenantError("org_id must be a UUID")
    session.info[_INFO_ORG] = org_id
    session.info[_INFO_SUBJECT] = auth_subject


def set_portal_subject(session: AsyncSession, subject: str | None) -> None:
    """Mark ``session`` as acting for one external portal person (``kind:<uuid>``), or
    clear it. Like ``set_tenant``: before the transaction begins, applies from the next
    transaction start. Workflows key restrictive RLS policies on ``app.portal_subject``
    so a portal session can only ever see that person's own rows."""
    if session.in_transaction():
        raise TenantError("set the portal subject before the transaction begins")
    if subject is not None and not _PORTAL_RE.fullmatch(subject):
        raise TenantError("invalid portal subject")
    session.info[_INFO_PORTAL] = subject


def current_portal_subject(session: AsyncSession | Session) -> str | None:
    value = session.info.get(_INFO_PORTAL)
    return value if isinstance(value, str) else None


@asynccontextmanager
async def outside_portal(session: AsyncSession) -> AsyncIterator[None]:
    """Run a system step of a portal request past the portal guard (migration 0022).

    Only for engine work a portal action causes but whose rows are not the portal
    person's and never go back to them: queueing a notification for staff (recipients,
    preferences, dedupe), finding the head of a decision chain. Inside the block
    ``app.portal_subject`` is empty for this transaction; the tenant boundary (org) is
    unchanged. Pending writes are flushed first, so they are still checked against the
    portal policies. Outside a portal session this does nothing."""
    subject = current_portal_subject(session)
    if subject is None:
        yield
        return
    await session.flush()
    await session.execute(select(func.set_config(PORTAL_SETTING, "", True)))
    try:
        yield
    finally:
        # If the transaction failed, it rolls back and the setting with it.
        with suppress(DBAPIError):
            await session.execute(select(func.set_config(PORTAL_SETTING, subject, True)))


def current_org(session: AsyncSession | Session) -> uuid.UUID | None:
    value = session.info.get(_INFO_ORG)
    return value if isinstance(value, uuid.UUID) else None


def _apply_settings(session: Session, _tx: SessionTransaction, conn: Connection) -> None:
    org = session.info.get(_INFO_ORG)
    subject = session.info.get(_INFO_SUBJECT)
    portal = session.info.get(_INFO_PORTAL)
    # Always set all three, so a value can never survive from an earlier context.
    conn.execute(
        select(
            func.set_config(ORG_SETTING, str(org) if org is not None else "", True),
            func.set_config(SUBJECT_SETTING, subject or "", True),
            func.set_config(PORTAL_SETTING, portal or "", True),
        )
    )


def _add_tenant_criteria(state: ORMExecuteState) -> None:
    if not (state.is_select or state.is_update or state.is_delete):
        return
    org = state.session.info.get(_INFO_ORG)
    if org is None:
        options = [
            with_loader_criteria(OrgScoped, lambda cls: false(), include_aliases=True),
            with_loader_criteria(OrgRoot, lambda cls: false(), include_aliases=True),
            with_loader_criteria(
                OrgScopedOrShared, lambda cls: cls.org_id.is_(None), include_aliases=True
            ),
        ]
    else:
        options = [
            with_loader_criteria(OrgScoped, lambda cls: cls.org_id == org, include_aliases=True),
            with_loader_criteria(OrgRoot, lambda cls: cls.id == org, include_aliases=True),
            with_loader_criteria(
                OrgScopedOrShared,
                lambda cls: or_(cls.org_id.is_(None), cls.org_id == org),
                include_aliases=True,
            ),
        ]
    state.statement = state.statement.options(*options)


def _guard_flush(session: Session, _ctx: Any, _instances: Any) -> None:
    org = session.info.get(_INFO_ORG)
    for obj in session.new:
        if isinstance(obj, OrgScoped | OrgScopedOrShared):
            if org is None:
                raise TenantError("cannot write a tenant row without a tenant context")
            if obj.org_id is None:
                obj.org_id = org
            elif obj.org_id != org:
                raise TenantError("cannot write a row for another organisation")
    for obj in session.dirty:
        if isinstance(obj, OrgScoped | OrgScopedOrShared) and obj.org_id != org:
            raise TenantError("cannot modify a row of another organisation")
        if isinstance(obj, OrgRoot) and obj.id != org:
            raise TenantError("cannot modify another organisation")
    for obj in session.deleted:
        if isinstance(obj, OrgScoped | OrgScopedOrShared) and obj.org_id != org:
            raise TenantError("cannot delete a row of another organisation")


class TenantSession(Session):
    """Sync session class behind every ``AsyncSession`` we create."""


event.listen(TenantSession, "after_begin", _apply_settings)
event.listen(TenantSession, "do_orm_execute", _add_tenant_criteria)
event.listen(TenantSession, "before_flush", _guard_flush)


# ------------------------------------------------------------------ engine


def create_engine(settings: Settings) -> AsyncEngine:
    return create_async_engine(
        settings.database_url.get_secret_value(),
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_pre_ping=True,
    )


def create_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(
        engine,
        expire_on_commit=False,
        autobegin=True,
        sync_session_class=TenantSession,
    )


class UnsafeDatabaseRoleError(RuntimeError):
    """The application connected as a role that bypasses Row Level Security."""


async def check_app_role(engine: AsyncEngine) -> None:
    """Refuse to run as a superuser or a BYPASSRLS role (RLS would not apply)."""
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT rolname, rolsuper, rolbypassrls FROM pg_roles "
                    "WHERE rolname = current_user"
                )
            )
        ).one()
    if row.rolsuper or row.rolbypassrls:
        raise UnsafeDatabaseRoleError(
            f"database role {row.rolname!r} bypasses row level security; "
            "PAIRLY_DATABASE_URL must use the non-owner application role"
        )


async def ping(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))
