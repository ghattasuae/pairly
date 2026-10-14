# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""The ``portal_guard_rules`` table (migration 0022): the narrow exceptions to the
portal guard, per table and kind of portal person. Only migrations read or write it
(the application role has no privilege on it); ``portal_guard_apply(table)`` rebuilds
the table's ``<table>_portal_guard`` policy from its rules."""

from __future__ import annotations

from sqlalchemy import String, Text
from sqlalchemy.orm import Mapped, mapped_column

from pairly_core.db import Base


class PortalGuardRule(Base):
    __tablename__ = "portal_guard_rules"

    table_name: Mapped[str] = mapped_column(String(63), primary_key=True)
    subject_kind: Mapped[str] = mapped_column(String(32), primary_key=True)
    using_sql: Mapped[str | None] = mapped_column(Text, nullable=True)
    check_sql: Mapped[str | None] = mapped_column(Text, nullable=True)
