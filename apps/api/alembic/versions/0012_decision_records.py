# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Decision records, replay, appeals, compliance packs (WP1.10) and audit hardening
(WP1.10b).

Every table is a tenant table (RLS forced, standard tenant policy).

* ``decision_records``: append only. The application role gets SELECT and INSERT.
  ``decision_records_guard`` rejects DELETE and TRUNCATE for every role, and any UPDATE
  except one that changes only the encrypted ``sensitive`` column (key rotation by the
  owner role, ``pairly-admin crypto rotate``; the application role has no UPDATE
  privilege at all).
* ``decision_replays``: append only (``workflow_reject_change``, migration 0006).
* ``appeals``: SELECT, INSERT and UPDATE of the resolution columns for the application
  role. ``appeals_guard`` rejects DELETE, changes to identity columns, and for the
  application role any change once an appeal is resolved.
* ``compliance_exports`` and ``audit_verifications``: job rows, SELECT, INSERT and
  UPDATE of the status columns.
* ``audit_denial_limits``: per-actor token buckets, SELECT, INSERT and UPDATE. Rate
  limit state only (the denials themselves are audit events), so it is deleted with
  its organisation.

Worker enumeration (SECURITY DEFINER, owned by the migration role, same pattern as
``approval_overdue_org_ids`` in 0006): ``appeal_overdue_org_ids()``,
``decision_jobs_pending_org_ids()`` and ``audit_denial_flush_org_ids()`` return
organisation ids only. They switch ``app.decision_scan`` on for their own query, and a
SELECT policy limited to the owner role admits rows while the flag is on.

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-07
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from pairly_core.migration_helpers import app_db_role, ident, org_rls_statements

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_KINDS = (
    "'score', 'shortlist_suggestion', 'fraud_flag', 'risk_score', 'extraction', "
    "'extraction_correction', 'policy_check', 'approval', 'final_action', 'appeal_outcome'"
)
_DECISION_ID = "'^dec_[0-9A-HJKMNP-TV-Z]{26}$'"
_HEX64 = "'^[0-9a-f]{64}$'"
_SCAN_FLAG = "app.decision_scan"
TABLES = (
    "decision_records",
    "decision_replays",
    "appeals",
    "compliance_exports",
    "audit_verifications",
    "audit_denial_limits",
)
_FUNCTIONS = (
    "appeal_overdue_org_ids()",
    "decision_jobs_pending_org_ids()",
    "audit_denial_flush_org_ids()",
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


def _user_fk(table: str, column: str, *, nullable: bool) -> sa.Column[Any]:
    return sa.Column(
        column,
        sa.Uuid(),
        sa.ForeignKey("users.id", ondelete="RESTRICT", name=f"fk_{table}_{column}_users"),
        nullable=nullable,
    )


def _jsonb(name: str, default: str) -> sa.Column[Any]:
    return sa.Column(
        name, postgresql.JSONB(), nullable=False, server_default=sa.text(f"'{default}'::jsonb")
    )


def _uuids(name: str) -> sa.Column[Any]:
    return sa.Column(
        name,
        postgresql.ARRAY(sa.Uuid()),
        nullable=False,
        server_default=sa.text("'{}'::uuid[]"),
    )


def _ts(name: str, *, nullable: bool = True, now: bool = False) -> sa.Column[Any]:
    return sa.Column(
        name,
        sa.DateTime(timezone=True),
        nullable=nullable,
        server_default=sa.func.now() if now else None,
    )


def _rls(table: str, app_role: str, privileges: str) -> None:
    op.create_index(f"ix_{table}_org_id", table, ["org_id"])
    for stmt in org_rls_statements(table, app_role, privileges=privileges):
        op.execute(stmt)


def _always(table: str, *triggers: str) -> None:
    for trigger in triggers:
        op.execute(f"ALTER TABLE {table} ENABLE ALWAYS TRIGGER {trigger}")


def upgrade() -> None:
    app_role = app_db_role()

    # ---------------------------------------------------------------- decision_records
    op.create_table(
        "decision_records",
        sa.Column("id", sa.String(30), primary_key=True),
        _org("decision_records"),
        sa.Column("workflow", sa.String(32), nullable=False),
        sa.Column("entity_type", sa.String(64), nullable=False),
        sa.Column("entity_id", sa.String(128), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("replay_handler", sa.String(64), nullable=True),
        _jsonb("inputs", "{}"),
        _jsonb("policy_versions", "[]"),
        sa.Column(
            "rubric_version_id",
            sa.Uuid(),
            sa.ForeignKey(
                "policy_versions.id",
                ondelete="RESTRICT",
                name="fk_decision_records_rubric_version_id_policy_versions",
            ),
            nullable=True,
        ),
        _jsonb("prompt_versions", "[]"),
        _uuids("model_call_ids"),
        _jsonb("model", "{}"),
        _jsonb("output", "{}"),
        sa.Column("confidence", sa.Float(), nullable=True),
        _jsonb("evidence", "{}"),
        sa.Column("sensitive", sa.Text(), nullable=True),
        sa.Column("final_action", sa.String(64), nullable=True),
        sa.Column("actor_type", sa.String(16), nullable=False),
        sa.Column("actor_id", sa.String(128), nullable=False),
        _ts("decided_at", nullable=False),
        _uuids("audit_event_ids"),
        _uuids("approval_ids"),
        sa.Column("supersedes", sa.String(30), nullable=True),
        sa.Column("content_hash", sa.String(64), nullable=False),
        _ts("created_at", nullable=False, now=True),
        sa.UniqueConstraint("org_id", "id", name="uq_decision_records_org_id_id"),
        sa.ForeignKeyConstraint(
            ["org_id", "supersedes"],
            ["decision_records.org_id", "decision_records.id"],
            ondelete="RESTRICT",
            name="fk_decision_records_supersedes",
        ),
        sa.CheckConstraint(f"id ~ {_DECISION_ID}", name="ck_decision_records_id_format"),
        sa.CheckConstraint(f"kind IN ({_KINDS})", name="ck_decision_records_kind_valid"),
        sa.CheckConstraint(
            "workflow ~ '^[a-z][a-z0-9_]{0,31}$'", name="ck_decision_records_workflow_format"
        ),
        sa.CheckConstraint(
            "actor_type IN ('user', 'agent', 'system')", name="ck_decision_records_actor_valid"
        ),
        sa.CheckConstraint(
            "confidence >= 0 AND confidence <= 1", name="ck_decision_records_confidence_range"
        ),
        sa.CheckConstraint(
            f"content_hash ~ {_HEX64}", name="ck_decision_records_content_hash_format"
        ),
        sa.CheckConstraint(
            "supersedes IS NULL OR supersedes <> id", name="ck_decision_records_not_self"
        ),
    )
    op.create_index(
        "ix_decision_records_entity",
        "decision_records",
        ["org_id", "entity_type", "entity_id", "created_at"],
    )
    op.create_index(
        "ix_decision_records_org_id_decided_at", "decision_records", ["org_id", "decided_at"]
    )
    op.create_index(
        "uq_decision_records_supersedes",
        "decision_records",
        ["org_id", "supersedes"],
        unique=True,
        postgresql_where=sa.text("supersedes IS NOT NULL"),
    )
    _rls("decision_records", app_role, "SELECT, INSERT")
    op.execute(
        """
        CREATE FUNCTION decision_records_guard() RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = pg_catalog, public
        AS $$
        BEGIN
            IF TG_OP = 'UPDATE'
               AND (to_jsonb(NEW) - 'sensitive') = (to_jsonb(OLD) - 'sensitive')
               AND (NEW.sensitive IS NULL) = (OLD.sensitive IS NULL) THEN
                -- Key rotation re-wraps the encrypted column and nothing else.
                RETURN NEW;
            END IF;
            RAISE EXCEPTION 'decision_records is append-only: % is not allowed', TG_OP
                USING ERRCODE = 'insufficient_privilege';
        END
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER decision_records_immutable BEFORE UPDATE OR DELETE ON decision_records "
        "FOR EACH ROW EXECUTE FUNCTION decision_records_guard()"
    )
    op.execute(
        "CREATE TRIGGER decision_records_no_truncate BEFORE TRUNCATE ON decision_records "
        "FOR EACH STATEMENT EXECUTE FUNCTION workflow_reject_change()"
    )
    _always("decision_records", "decision_records_immutable", "decision_records_no_truncate")
    op.execute("REVOKE ALL ON FUNCTION decision_records_guard() FROM PUBLIC")

    # ---------------------------------------------------------------- decision_replays
    op.create_table(
        "decision_replays",
        _pk(),
        _org("decision_replays"),
        sa.Column("decision_id", sa.String(30), nullable=False),
        sa.Column("mode", sa.String(16), nullable=False),
        sa.Column("deterministic", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("reason", sa.String(128), nullable=True),
        _jsonb("diff", "[]"),
        _jsonb("details", "{}"),
        sa.Column("requested_by_type", sa.String(16), nullable=False),
        sa.Column("requested_by_id", sa.String(128), nullable=False),
        _ts("created_at", nullable=False, now=True),
        sa.ForeignKeyConstraint(
            ["org_id", "decision_id"],
            ["decision_records.org_id", "decision_records.id"],
            ondelete="RESTRICT",
            name="fk_decision_replays_decision",
        ),
        sa.CheckConstraint("mode IN ('recorded', 'live')", name="ck_decision_replays_mode_valid"),
        sa.CheckConstraint(
            "status IN ('match', 'differs', 'failed')", name="ck_decision_replays_status_valid"
        ),
        sa.CheckConstraint(
            "requested_by_type IN ('user', 'agent', 'system')",
            name="ck_decision_replays_requester_valid",
        ),
        sa.CheckConstraint(
            "status <> 'failed' OR reason IS NOT NULL",
            name="ck_decision_replays_failed_has_reason",
        ),
    )
    op.create_index(
        "ix_decision_replays_decision",
        "decision_replays",
        ["org_id", "decision_id", "created_at"],
    )
    _rls("decision_replays", app_role, "SELECT, INSERT")
    op.execute(
        "CREATE TRIGGER decision_replays_immutable BEFORE UPDATE OR DELETE ON decision_replays "
        "FOR EACH ROW EXECUTE FUNCTION workflow_reject_change()"
    )
    op.execute(
        "CREATE TRIGGER decision_replays_no_truncate BEFORE TRUNCATE ON decision_replays "
        "FOR EACH STATEMENT EXECUTE FUNCTION workflow_reject_change()"
    )
    _always("decision_replays", "decision_replays_immutable", "decision_replays_no_truncate")

    # ---------------------------------------------------------------- appeals
    op.create_table(
        "appeals",
        _pk(),
        _org("appeals"),
        sa.Column("decision_id", sa.String(30), nullable=False),
        sa.Column("requester_type", sa.String(16), nullable=False),
        sa.Column("requester_id", sa.String(128), nullable=False),
        _user_fk("appeals", "filed_by", nullable=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("outcome", sa.String(24), nullable=True),
        sa.Column("outcome_text", sa.Text(), nullable=True),
        _user_fk("appeals", "decided_by", nullable=True),
        _ts("decided_at"),
        sa.Column("outcome_decision_id", sa.String(30), nullable=True),
        _ts("sla_due_at", nullable=False),
        _ts("escalated_at"),
        _ts("created_at", nullable=False, now=True),
        _ts("updated_at", nullable=False, now=True),
        sa.UniqueConstraint("org_id", "id", name="uq_appeals_org_id_id"),
        sa.ForeignKeyConstraint(
            ["org_id", "decision_id"],
            ["decision_records.org_id", "decision_records.id"],
            ondelete="RESTRICT",
            name="fk_appeals_decision",
        ),
        sa.CheckConstraint("status IN ('open', 'resolved')", name="ck_appeals_status_valid"),
        sa.CheckConstraint(
            "outcome IS NULL OR outcome IN ('upheld', 'overturned', 'partially_overturned')",
            name="ck_appeals_outcome_valid",
        ),
        sa.CheckConstraint(
            "requester_type IN ('candidate', 'vendor', 'staff')", name="ck_appeals_requester_valid"
        ),
        sa.CheckConstraint(
            "(status = 'resolved') = (outcome IS NOT NULL AND outcome_text IS NOT NULL "
            "AND decided_by IS NOT NULL AND decided_at IS NOT NULL)",
            name="ck_appeals_resolution_complete",
        ),
    )
    op.create_index("ix_appeals_queue", "appeals", ["org_id", "status", "sla_due_at"])
    op.create_index("ix_appeals_decision", "appeals", ["org_id", "decision_id"])
    _rls("appeals", app_role, "SELECT, INSERT")
    op.execute(
        "GRANT UPDATE (status, outcome, outcome_text, decided_by, decided_at, "
        f"outcome_decision_id, escalated_at, updated_at) ON appeals TO {app_role}"
    )
    op.execute(
        f"""
        CREATE FUNCTION appeals_guard() RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = pg_catalog, public
        AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'appeals rows cannot be deleted'
                    USING ERRCODE = 'insufficient_privilege';
            END IF;
            IF OLD.status = 'resolved' AND current_user = '{ident(app_role)}' THEN
                RAISE EXCEPTION 'a resolved appeal cannot change'
                    USING ERRCODE = 'insufficient_privilege';
            END IF;
            IF OLD.status = 'resolved'
               AND (to_jsonb(NEW) - 'reason' - 'outcome_text')
                   <> (to_jsonb(OLD) - 'reason' - 'outcome_text') THEN
                RAISE EXCEPTION 'a resolved appeal cannot change'
                    USING ERRCODE = 'insufficient_privilege';
            END IF;
            IF NEW.id IS DISTINCT FROM OLD.id
               OR NEW.org_id IS DISTINCT FROM OLD.org_id
               OR NEW.decision_id IS DISTINCT FROM OLD.decision_id
               OR NEW.requester_type IS DISTINCT FROM OLD.requester_type
               OR NEW.requester_id IS DISTINCT FROM OLD.requester_id
               OR NEW.filed_by IS DISTINCT FROM OLD.filed_by
               OR NEW.sla_due_at IS DISTINCT FROM OLD.sla_due_at
               OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
                RAISE EXCEPTION 'appeal identity columns cannot change'
                    USING ERRCODE = 'insufficient_privilege';
            END IF;
            RETURN NEW;
        END
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER appeals_guard BEFORE UPDATE OR DELETE ON appeals "
        "FOR EACH ROW EXECUTE FUNCTION appeals_guard()"
    )
    op.execute(
        "CREATE TRIGGER appeals_no_truncate BEFORE TRUNCATE ON appeals "
        "FOR EACH STATEMENT EXECUTE FUNCTION workflow_reject_change()"
    )
    _always("appeals", "appeals_guard", "appeals_no_truncate")
    op.execute("REVOKE ALL ON FUNCTION appeals_guard() FROM PUBLIC")

    # ---------------------------------------------------------------- compliance_exports
    op.create_table(
        "compliance_exports",
        _pk(),
        _org("compliance_exports"),
        _user_fk("compliance_exports", "requested_by", nullable=False),
        _ts("period_start", nullable=False),
        _ts("period_end", nullable=False),
        sa.Column(
            "workflows",
            postgresql.ARRAY(sa.String(32)),
            nullable=False,
            server_default=sa.text("'{}'::varchar[]"),
        ),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("storage_key", sa.String(256), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("sha256", sa.String(64), nullable=True),
        sa.Column("manifest_sha256", sa.String(64), nullable=True),
        sa.Column("manifest", postgresql.JSONB(), nullable=True),
        sa.Column("error", sa.String(128), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        _ts("created_at", nullable=False, now=True),
        _ts("started_at"),
        _ts("completed_at"),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'ready', 'failed')",
            name="ck_compliance_exports_status_valid",
        ),
        sa.CheckConstraint("period_end > period_start", name="ck_compliance_exports_period_valid"),
        sa.CheckConstraint(
            f"sha256 IS NULL OR sha256 ~ {_HEX64}", name="ck_compliance_exports_sha256_format"
        ),
        sa.CheckConstraint(
            f"manifest_sha256 IS NULL OR manifest_sha256 ~ {_HEX64}",
            name="ck_compliance_exports_manifest_sha256_format",
        ),
    )
    op.create_index(
        "ix_compliance_exports_status", "compliance_exports", ["org_id", "status", "created_at"]
    )
    _rls("compliance_exports", app_role, "SELECT, INSERT")
    op.execute(
        "GRANT UPDATE (status, storage_key, size_bytes, sha256, manifest_sha256, manifest, "
        f"error, attempts, started_at, completed_at) ON compliance_exports TO {app_role}"
    )

    # ---------------------------------------------------------------- audit_verifications
    op.create_table(
        "audit_verifications",
        _pk(),
        _org("audit_verifications"),
        sa.Column("requested_by_type", sa.String(16), nullable=False),
        sa.Column("requested_by_id", sa.String(128), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("result", postgresql.JSONB(), nullable=True),
        sa.Column("error", sa.String(128), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        _ts("created_at", nullable=False, now=True),
        _ts("started_at"),
        _ts("completed_at"),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'done', 'failed')",
            name="ck_audit_verifications_status_valid",
        ),
        sa.CheckConstraint(
            "requested_by_type IN ('user', 'agent', 'system')",
            name="ck_audit_verifications_requester_valid",
        ),
    )
    op.create_index(
        "ix_audit_verifications_status",
        "audit_verifications",
        ["org_id", "status", "created_at"],
    )
    _rls("audit_verifications", app_role, "SELECT, INSERT")
    op.execute(
        "GRANT UPDATE (status, result, error, attempts, started_at, completed_at) "
        f"ON audit_verifications TO {app_role}"
    )

    # ---------------------------------------------------------------- audit_denial_limits
    op.create_table(
        "audit_denial_limits",
        _pk(),
        # Rate limit state only, no history: it goes with its organisation.
        sa.Column(
            "org_id",
            sa.Uuid(),
            sa.ForeignKey(
                "organisations.id",
                ondelete="CASCADE",
                name="fk_audit_denial_limits_org_id_organisations",
            ),
            nullable=False,
        ),
        sa.Column("actor_key", sa.String(160), nullable=False),
        sa.Column("tokens", sa.Float(), nullable=False),
        _ts("updated_at", nullable=False),
        sa.Column("suppressed", sa.Integer(), nullable=False, server_default="0"),
        _ts("suppressed_since"),
        _jsonb("suppressed_permissions", "[]"),
        sa.UniqueConstraint("org_id", "actor_key", name="uq_audit_denial_limits_actor"),
        sa.CheckConstraint("tokens >= 0", name="ck_audit_denial_limits_tokens_non_negative"),
        sa.CheckConstraint(
            "suppressed >= 0", name="ck_audit_denial_limits_suppressed_non_negative"
        ),
    )
    op.create_index(
        "ix_audit_denial_limits_pending",
        "audit_denial_limits",
        ["org_id", "suppressed", "updated_at"],
    )
    _rls("audit_denial_limits", app_role, "SELECT, INSERT, UPDATE")

    # ---------------------------------------------------------------- worker enumeration
    for table in ("appeals", "compliance_exports", "audit_verifications", "audit_denial_limits"):
        op.execute(
            f"CREATE POLICY {table}_worker_scan ON {table} FOR SELECT TO CURRENT_USER "
            f"USING (current_setting('{_SCAN_FLAG}', true) = 'on')"
        )
    queries = {
        "appeal_overdue_org_ids": (
            "SELECT array_agg(DISTINCT a.org_id ORDER BY a.org_id) INTO ids "
            "FROM public.appeals a WHERE a.status = 'open' AND a.escalated_at IS NULL "
            "AND a.sla_due_at < now();"
        ),
        # Jobs nobody picked up within two minutes (lost enqueue or a crashed worker).
        "decision_jobs_pending_org_ids": (
            "SELECT array_agg(DISTINCT j.org_id ORDER BY j.org_id) INTO ids FROM ("
            "SELECT e.org_id FROM public.compliance_exports e "
            "WHERE e.status IN ('pending', 'running') "
            "AND coalesce(e.started_at, e.created_at) < now() - interval '2 minutes' "
            "UNION ALL SELECT v.org_id FROM public.audit_verifications v "
            "WHERE v.status IN ('pending', 'running') "
            "AND coalesce(v.started_at, v.created_at) < now() - interval '2 minutes') j;"
        ),
        "audit_denial_flush_org_ids": (
            "SELECT array_agg(DISTINCT d.org_id ORDER BY d.org_id) INTO ids "
            "FROM public.audit_denial_limits d WHERE d.suppressed > 0;"
        ),
    }
    for name, query in queries.items():
        op.execute(
            f"""
            CREATE FUNCTION {name}() RETURNS SETOF uuid
            LANGUAGE plpgsql VOLATILE
            SECURITY DEFINER
            SET search_path = pg_catalog, public
            AS $$
            DECLARE
                previous text := current_setting('{_SCAN_FLAG}', true);
                ids uuid[];
            BEGIN
                PERFORM set_config('{_SCAN_FLAG}', 'on', true);
                {query}
                PERFORM set_config('{_SCAN_FLAG}', coalesce(previous, ''), true);
                RETURN QUERY SELECT unnest(coalesce(ids, '{{}}'::uuid[]));
            END
            $$
            """
        )
    for fn in _FUNCTIONS:
        op.execute(f"REVOKE ALL ON FUNCTION {fn} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION {fn} TO {app_role}")


def downgrade() -> None:
    for fn in _FUNCTIONS:
        op.execute(f"DROP FUNCTION IF EXISTS {fn}")
    for table in reversed(TABLES):
        op.drop_table(table)
    op.execute("DROP FUNCTION IF EXISTS appeals_guard()")
    op.execute("DROP FUNCTION IF EXISTS decision_records_guard()")
