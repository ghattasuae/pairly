# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""``proposals`` (migration 0024). Org scoped, RLS forced, closed to portal sessions.

Append only: the application role may SELECT and INSERT, nothing else. A proposal is
never edited or withdrawn; a person acts on it (or not) through their own routes.
"""

from __future__ import annotations

import uuid

from sqlalchemy import CheckConstraint, Index, String, Text, Uuid, text
from sqlalchemy.orm import Mapped, mapped_column

from pairly_core.db import Base, OrgScoped, Timestamped


class Proposal(Base, OrgScoped, Timestamped):
    __tablename__ = "proposals"
    __table_args__ = (
        Index("ix_proposals_entity", "org_id", "entity_type", "entity_id", "created_at"),
        CheckConstraint("kind IN ('note', 'action')", name="kind_valid"),
        CheckConstraint("(kind = 'action') = (action IS NOT NULL)", name="action_only_for_actions"),
        CheckConstraint(
            "action IS NULL OR action ~ '^[a-z][a-z0-9_]{0,63}$'", name="action_format"
        ),
        CheckConstraint(
            "entity_type ~ '^[a-z][a-z0-9_]*([.][a-z][a-z0-9_]*){0,3}$' "
            "AND char_length(entity_type) <= 64",
            name="entity_type_format",
        ),
        CheckConstraint("char_length(entity_id) BETWEEN 1 AND 128", name="entity_id_length"),
        CheckConstraint("char_length(body) BETWEEN 1 AND 4000", name="body_length"),
        CheckConstraint(
            "workflow IS NULL OR workflow ~ '^[a-z][a-z0-9_]{0,31}$'", name="workflow_format"
        ),
        CheckConstraint(
            "proposed_by_type IN ('user', 'agent', 'api_key')", name="proposed_by_type_valid"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4, server_default=text("gen_random_uuid()")
    )
    workflow: Mapped[str | None] = mapped_column(String(32), nullable=True)
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(128), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    action: Mapped[str | None] = mapped_column(String(64), nullable=True)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    proposed_by_type: Mapped[str] = mapped_column(String(16), nullable=False)
    proposed_by_id: Mapped[str] = mapped_column(String(64), nullable=False)
