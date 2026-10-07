# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Extraction (WP1.3) and risk scoring (WP1.6) tables.

Every table is a tenant table (RLS forced, standard tenant policy) and append only: the
application role gets SELECT and INSERT, and ``workflow_reject_change()`` (migration
0006) rejects UPDATE, DELETE and TRUNCATE for every role.

* ``extractions``, ``extraction_fields``, ``extraction_corrections``
* ``risk_assessments``, ``risk_signals``

Children reference their parent by ``(org_id, id)``, so a row can never point at another
organisation's parent even though foreign key checks ignore RLS.

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-07
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from pairly_core.migration_helpers import app_db_role, org_rls_statements

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_WORKFLOW_RE = "'^[a-z][a-z0-9_]{0,31}$'"
_LEVELS = "'info', 'low', 'medium', 'high', 'critical'"
TABLES = (
    "extractions",
    "extraction_fields",
    "extraction_corrections",
    "risk_assessments",
    "risk_signals",
)


def _pk() -> sa.Column[Any]:
    return sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()"))


def _org(table: str) -> sa.Column[Any]:
    return sa.Column(
        "org_id",
        sa.Uuid(),
        sa.ForeignKey(
            "organisations.id", ondelete="RESTRICT", name=f"fk_{table}_org_id_organisations"
        ),
        nullable=False,
    )


def _jsonb_list(name: str) -> sa.Column[Any]:
    return sa.Column(
        name, postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")
    )


def _created_at() -> sa.Column[Any]:
    return sa.Column(
        "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )


def _creator(table: str) -> list[Any]:
    return [
        sa.Column("created_by_type", sa.String(16), nullable=False),
        sa.Column("created_by_id", sa.String(128), nullable=False),
        sa.CheckConstraint(
            "created_by_type IN ('user', 'agent', 'system')", name=f"ck_{table}_creator_valid"
        ),
    ]


def _secure(table: str, app_role: str) -> None:
    op.create_index(f"ix_{table}_org_id", table, ["org_id"])
    for stmt in org_rls_statements(table, app_role, privileges="SELECT, INSERT"):
        op.execute(stmt)
    op.execute(
        f"CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE ON {table} "
        "FOR EACH ROW EXECUTE FUNCTION workflow_reject_change()"
    )
    op.execute(
        f"CREATE TRIGGER {table}_no_truncate BEFORE TRUNCATE ON {table} "
        "FOR EACH STATEMENT EXECUTE FUNCTION workflow_reject_change()"
    )
    for trigger in (f"{table}_immutable", f"{table}_no_truncate"):
        op.execute(f"ALTER TABLE {table} ENABLE ALWAYS TRIGGER {trigger}")


def upgrade() -> None:
    app_role = app_db_role()

    # ---------------------------------------------------------------- extractions
    op.create_table(
        "extractions",
        _pk(),
        _org("extractions"),
        sa.Column(
            "document_id",
            sa.Uuid(),
            sa.ForeignKey(
                "documents.id", ondelete="RESTRICT", name="fk_extractions_document_id_documents"
            ),
            nullable=False,
        ),
        sa.Column("workflow", sa.String(32), nullable=False),
        sa.Column("schema_name", sa.String(96), nullable=False),
        sa.Column("schema_version", sa.String(32), nullable=False),
        sa.Column("field_specs", postgresql.JSONB(), nullable=False),
        sa.Column("prompt_task", sa.String(64), nullable=False),
        sa.Column("prompt_version", sa.String(32), nullable=False),
        sa.Column("prompt_hash", sa.String(64), nullable=True),
        sa.Column("model_provider", sa.String(32), nullable=True),
        sa.Column("model_name", sa.String(128), nullable=True),
        sa.Column(
            "model_call_ids",
            postgresql.ARRAY(sa.Uuid()),
            nullable=False,
            server_default=sa.text("'{}'::uuid[]"),
        ),
        sa.Column("model_chain_id", sa.Uuid(), nullable=True),
        sa.Column("from_recording", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("status", sa.String(16), nullable=False),
        _jsonb_list("reasons"),
        _jsonb_list("ignored_paths"),
        sa.Column("template_key", sa.String(128), nullable=True),
        *_creator("extractions"),
        _created_at(),
        sa.UniqueConstraint("org_id", "id", name="uq_extractions_org_id_id"),
        sa.CheckConstraint(f"workflow ~ {_WORKFLOW_RE}", name="ck_extractions_workflow_format"),
        sa.CheckConstraint("status IN ('ok', 'needs_human')", name="ck_extractions_status_valid"),
    )
    op.create_index("ix_extractions_org_id_document_id", "extractions", ["org_id", "document_id"])
    _secure("extractions", app_role)

    op.create_table(
        "extraction_fields",
        _pk(),
        _org("extraction_fields"),
        sa.Column("extraction_id", sa.Uuid(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("path", sa.String(256), nullable=False),
        sa.Column("pattern", sa.String(256), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("value", postgresql.JSONB(), nullable=True),
        sa.Column("raw_value", sa.Text(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("threshold", sa.Float(), nullable=False),
        sa.Column("required", sa.Boolean(), nullable=False),
        sa.Column("needs_correction", sa.Boolean(), nullable=False),
        _jsonb_list("flags"),
        _jsonb_list("spans"),
        sa.ForeignKeyConstraint(
            ["org_id", "extraction_id"],
            ["extractions.org_id", "extractions.id"],
            ondelete="RESTRICT",
            name="fk_extraction_fields_extraction",
        ),
        sa.UniqueConstraint(
            "extraction_id", "path", name="uq_extraction_fields_extraction_id_path"
        ),
        sa.CheckConstraint(
            "confidence >= 0 AND confidence <= 1", name="ck_extraction_fields_confidence_range"
        ),
        sa.CheckConstraint(
            "threshold >= 0 AND threshold <= 1", name="ck_extraction_fields_threshold_range"
        ),
    )
    _secure("extraction_fields", app_role)

    op.create_table(
        "extraction_corrections",
        _pk(),
        _org("extraction_corrections"),
        sa.Column("extraction_id", sa.Uuid(), nullable=False),
        sa.Column("field_path", sa.String(256), nullable=False),
        sa.Column("field_pattern", sa.String(256), nullable=False),
        sa.Column("old_value", postgresql.JSONB(), nullable=True),
        sa.Column("new_value", postgresql.JSONB(), nullable=True),
        sa.Column(
            "corrected_by",
            sa.Uuid(),
            sa.ForeignKey(
                "users.id", ondelete="RESTRICT", name="fk_extraction_corrections_corrected_by_users"
            ),
            nullable=False,
        ),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("workflow", sa.String(32), nullable=False),
        sa.Column("schema_name", sa.String(96), nullable=False),
        sa.Column("template_key", sa.String(128), nullable=True),
        _created_at(),
        sa.ForeignKeyConstraint(
            ["org_id", "extraction_id"],
            ["extractions.org_id", "extractions.id"],
            ondelete="RESTRICT",
            name="fk_extraction_corrections_extraction",
        ),
        sa.CheckConstraint(
            f"workflow ~ {_WORKFLOW_RE}", name="ck_extraction_corrections_workflow_format"
        ),
        sa.CheckConstraint(
            "length(reason) BETWEEN 1 AND 1000", name="ck_extraction_corrections_reason_length"
        ),
    )
    op.create_index(
        "ix_extraction_corrections_hints",
        "extraction_corrections",
        ["org_id", "workflow", "schema_name", "template_key", "created_at"],
    )
    op.create_index(
        "ix_extraction_corrections_extraction",
        "extraction_corrections",
        ["org_id", "extraction_id", "created_at"],
    )
    _secure("extraction_corrections", app_role)

    # ---------------------------------------------------------------- risk
    op.create_table(
        "risk_assessments",
        _pk(),
        _org("risk_assessments"),
        sa.Column("workflow", sa.String(32), nullable=False),
        sa.Column("assessment_type", sa.String(96), nullable=False),
        sa.Column("entity_type", sa.String(64), nullable=False),
        sa.Column("entity_id", sa.String(128), nullable=False),
        sa.Column(
            "policy_version_id",
            sa.Uuid(),
            sa.ForeignKey(
                "policy_versions.id",
                ondelete="RESTRICT",
                name="fk_risk_assessments_policy_version_id_policy_versions",
            ),
            nullable=True,
        ),
        sa.Column("policy_version", sa.Integer(), nullable=True),
        sa.Column("policy_hash", sa.String(64), nullable=True),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("level", sa.String(16), nullable=False),
        sa.Column("requires_human", sa.Boolean(), nullable=False),
        _jsonb_list("reasons"),
        *_creator("risk_assessments"),
        _created_at(),
        sa.UniqueConstraint("org_id", "id", name="uq_risk_assessments_org_id_id"),
        sa.CheckConstraint(
            f"workflow ~ {_WORKFLOW_RE}", name="ck_risk_assessments_workflow_format"
        ),
        sa.CheckConstraint("score >= 0 AND score <= 1", name="ck_risk_assessments_score_range"),
        sa.CheckConstraint(f"level IN ({_LEVELS})", name="ck_risk_assessments_level_valid"),
        sa.CheckConstraint(
            "policy_version_id IS NOT NULL OR requires_human",
            name="ck_risk_assessments_policy_or_human",
        ),
    )
    op.create_index(
        "ix_risk_assessments_entity",
        "risk_assessments",
        ["org_id", "entity_type", "entity_id", "created_at"],
    )
    _secure("risk_assessments", app_role)

    op.create_table(
        "risk_signals",
        _pk(),
        _org("risk_signals"),
        sa.Column("assessment_id", sa.Uuid(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("detector", sa.String(96), nullable=False),
        sa.Column("detector_version", sa.String(32), nullable=False),
        sa.Column("status", sa.String(8), nullable=False),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("weight", sa.Float(), nullable=False),
        sa.Column("label", sa.String(64), nullable=False),
        sa.Column("severity", sa.String(16), nullable=False),
        _jsonb_list("evidence"),
        sa.Column("explanation_key", sa.String(128), nullable=False),
        sa.ForeignKeyConstraint(
            ["org_id", "assessment_id"],
            ["risk_assessments.org_id", "risk_assessments.id"],
            ondelete="RESTRICT",
            name="fk_risk_signals_assessment",
        ),
        sa.UniqueConstraint(
            "assessment_id", "position", name="uq_risk_signals_assessment_position"
        ),
        sa.CheckConstraint("score >= 0 AND score <= 1", name="ck_risk_signals_score_range"),
        sa.CheckConstraint("weight >= 0 AND weight <= 1", name="ck_risk_signals_weight_range"),
        sa.CheckConstraint("status IN ('ok', 'error')", name="ck_risk_signals_status_valid"),
        sa.CheckConstraint(f"severity IN ({_LEVELS})", name="ck_risk_signals_severity_valid"),
    )
    _secure("risk_signals", app_role)


def downgrade() -> None:
    for table in reversed(TABLES):
        op.drop_table(table)
