# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Partial embedding billing and the policy guardrail override.

* ``model_calls.status`` also accepts ``partial_error``: a batched embedding call that
  failed after some batches were embedded (and billed by the provider) records the
  usage and cost of the successful batches. The downgrade turns ``partial_error`` rows
  into ``provider_error`` (the cost stays).
* ``policy_documents`` override columns: ``override_requested_by/_at/_reason`` (the
  first admin of a two-person override) and ``overridden_by/_at``, ``override_reason``
  (the admin who approved it). The application role may UPDATE them.
  ``policy_documents_guard`` replaces the 0010 version: ``needs_review`` may become
  ``pending`` only with a recorded override, and when any guardrail finding is high
  severity (or the guardrails could not run) only when the override was requested and
  approved by two different people. Override values are write once, and the findings
  and reason of a ``needs_review`` document are frozen, so the rule cannot be dodged by
  editing them first.

Revision ID: 0013
Revises: 0010
Create Date: 2026-10-07
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from pairly_core.migration_helpers import app_db_role, ident

revision: str = "0013"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CALL_STATUS_OLD = "status IN ('ok', 'invalid_output', 'low_confidence', 'provider_error')"
_CALL_STATUS_NEW = (
    "status IN ('ok', 'invalid_output', 'low_confidence', 'provider_error', 'partial_error')"
)


def _upgrade_model_calls() -> None:
    op.drop_constraint("ck_model_calls_status", "model_calls", type_="check")
    op.create_check_constraint("ck_model_calls_status", "model_calls", _CALL_STATUS_NEW)


def _downgrade_model_calls() -> None:
    # RLS is forced on the owner too: lift it for this migration's transaction.
    op.execute("ALTER TABLE model_calls NO FORCE ROW LEVEL SECURITY")
    op.execute("UPDATE model_calls SET status = 'provider_error' WHERE status = 'partial_error'")
    op.execute("ALTER TABLE model_calls FORCE ROW LEVEL SECURITY")
    op.drop_constraint("ck_model_calls_status", "model_calls", type_="check")
    op.create_check_constraint("ck_model_calls_status", "model_calls", _CALL_STATUS_OLD)


# ---------------------------------------------------------------- policy override

_OVERRIDE_COLUMNS = (
    "override_requested_by",
    "override_requested_at",
    "override_request_reason",
    "overridden_by",
    "overridden_at",
    "override_reason",
)

_GUARD_HEAD = """
CREATE OR REPLACE FUNCTION policy_documents_guard() RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, public
AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'policy_documents rows cannot be deleted'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF OLD.status = 'archived' THEN
        RAISE EXCEPTION 'an archived policy document cannot change'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
"""

_GUARD_IDENTITY = """
    IF NEW.id IS DISTINCT FROM OLD.id
       OR NEW.org_id IS DISTINCT FROM OLD.org_id
       OR NEW.name IS DISTINCT FROM OLD.name
       OR NEW.version IS DISTINCT FROM OLD.version
       OR NEW.title IS DISTINCT FROM OLD.title
       OR NEW.document_id IS DISTINCT FROM OLD.document_id
       OR NEW.created_by_type IS DISTINCT FROM OLD.created_by_type
       OR NEW.created_by_id IS DISTINCT FROM OLD.created_by_id
       OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
        RAISE EXCEPTION 'policy document identity columns cannot change'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    RETURN NEW;
END
$$
"""

_GUARD_0010 = (
    _GUARD_HEAD
    + """
    IF OLD.status = 'needs_review' AND NEW.status NOT IN ('needs_review', 'archived') THEN
        RAISE EXCEPTION 'a policy document that needs review cannot be activated'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
"""
    + _GUARD_IDENTITY
)

_GUARD_0013 = (
    _GUARD_HEAD
    + """
    -- Override values are write once.
    IF (OLD.override_requested_by IS NOT NULL
        AND (NEW.override_requested_by IS DISTINCT FROM OLD.override_requested_by
             OR NEW.override_requested_at IS DISTINCT FROM OLD.override_requested_at
             OR NEW.override_request_reason IS DISTINCT FROM OLD.override_request_reason))
       OR (OLD.overridden_by IS NOT NULL
        AND (NEW.overridden_by IS DISTINCT FROM OLD.overridden_by
             OR NEW.overridden_at IS DISTINCT FROM OLD.overridden_at
             OR NEW.override_reason IS DISTINCT FROM OLD.override_reason)) THEN
        RAISE EXCEPTION 'a policy document override cannot change'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF OLD.status <> 'needs_review'
       AND (NEW.override_requested_by IS DISTINCT FROM OLD.override_requested_by
            OR NEW.overridden_by IS DISTINCT FROM OLD.overridden_by) THEN
        RAISE EXCEPTION 'only a policy document that needs review can be overridden'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF OLD.status = 'needs_review' AND NEW.status = 'needs_review'
       AND (NEW.guardrail_findings IS DISTINCT FROM OLD.guardrail_findings
            OR NEW.guardrail_version IS DISTINCT FROM OLD.guardrail_version
            OR NEW.status_reason IS DISTINCT FROM OLD.status_reason) THEN
        RAISE EXCEPTION 'the findings of a policy document that needs review cannot change'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF OLD.status = 'needs_review' AND NEW.status NOT IN ('needs_review', 'archived') THEN
        IF NEW.status <> 'pending' OR NEW.overridden_by IS NULL THEN
            RAISE EXCEPTION 'a policy document that needs review cannot be activated'
                USING ERRCODE = 'insufficient_privilege';
        END IF;
        -- Two-person rule: high-severity findings, or guardrails that could not run.
        IF (OLD.status_reason = 'guardrails_error'
            OR jsonb_path_exists(OLD.guardrail_findings, '$[*] ? (@.severity == "high")'))
           AND (NEW.override_requested_by IS NULL
                OR NEW.override_requested_by = NEW.overridden_by) THEN
            RAISE EXCEPTION 'a second admin must approve this override'
                USING ERRCODE = 'insufficient_privilege';
        END IF;
    END IF;
"""
    + _GUARD_IDENTITY
)


def _upgrade_policy_override(role: str) -> None:
    for prefix in ("override_requested", "overridden"):
        op.add_column("policy_documents", sa.Column(f"{prefix}_by", sa.Uuid(), nullable=True))
        op.add_column(
            "policy_documents",
            sa.Column(f"{prefix}_at", sa.DateTime(timezone=True), nullable=True),
        )
        op.create_foreign_key(
            f"fk_policy_documents_{prefix}_by_users",
            "policy_documents",
            "users",
            [f"{prefix}_by"],
            ["id"],
            ondelete="RESTRICT",
        )
    op.add_column("policy_documents", sa.Column("override_request_reason", sa.Text()))
    op.add_column("policy_documents", sa.Column("override_reason", sa.Text()))
    op.create_check_constraint(
        "ck_policy_documents_override_request_complete",
        "policy_documents",
        "(override_requested_by IS NULL) = (override_requested_at IS NULL) "
        "AND (override_requested_by IS NULL) = (override_request_reason IS NULL)",
    )
    op.create_check_constraint(
        "ck_policy_documents_override_complete",
        "policy_documents",
        "(overridden_by IS NULL) = (overridden_at IS NULL) "
        "AND (overridden_by IS NULL) = (override_reason IS NULL)",
    )
    op.execute(f"GRANT UPDATE ({', '.join(_OVERRIDE_COLUMNS)}) ON policy_documents TO {role}")
    op.execute(_GUARD_0013)


def _downgrade_policy_override(role: str) -> None:
    op.execute(_GUARD_0010)
    op.execute(f"REVOKE UPDATE ({', '.join(_OVERRIDE_COLUMNS)}) ON policy_documents FROM {role}")
    op.drop_constraint(
        "ck_policy_documents_override_complete", "policy_documents", type_="check"
    )
    op.drop_constraint(
        "ck_policy_documents_override_request_complete", "policy_documents", type_="check"
    )
    for prefix in ("override_requested", "overridden"):
        op.drop_constraint(
            f"fk_policy_documents_{prefix}_by_users", "policy_documents", type_="foreignkey"
        )
    for column in _OVERRIDE_COLUMNS:
        op.drop_column("policy_documents", column)


def upgrade() -> None:
    role = ident(app_db_role())
    _upgrade_model_calls()
    _upgrade_policy_override(role)


def downgrade() -> None:
    role = ident(app_db_role())
    _downgrade_policy_override(role)
    _downgrade_model_calls()
