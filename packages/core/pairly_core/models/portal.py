# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""The ``portal_guard_rules`` table (migrations 0022 and 0023): the narrow exceptions to
the portal guard, per table and kind of portal person, each naming the commands it
allows. Only migrations read or write it (the application role has no privilege on
it); ``portal_guard_apply(table)`` rebuilds the table's restrictive policies
``<table>_portal_guard_select``, ``_insert``, ``_update`` and ``_delete`` from its
rules."""

from __future__ import annotations

from sqlalchemy import CheckConstraint, String, Text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from pairly_core.db import Base


class PortalGuardRule(Base):
    __tablename__ = "portal_guard_rules"
    __table_args__ = (
        CheckConstraint(
            "commands <@ ARRAY['SELECT', 'INSERT', 'UPDATE', 'DELETE']::text[]",
            name="commands_valid",
        ),
    )

    table_name: Mapped[str] = mapped_column(String(63), primary_key=True)
    subject_kind: Mapped[str] = mapped_column(String(32), primary_key=True)
    using_sql: Mapped[str | None] = mapped_column(Text, nullable=True)
    check_sql: Mapped[str | None] = mapped_column(Text, nullable=True)
    # SELECT, INSERT, UPDATE, DELETE: what the rule allows (filled from using/check by a
    # trigger when a rule is written without it).
    commands: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False)
