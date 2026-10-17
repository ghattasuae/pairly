# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Workflow runtime, approvals, signed links, policy versions (WP1.8).

Tenant tables (RLS forced, standard tenant policy, least privilege for the app role):

* ``policy_versions``: immutable policy documents (SELECT, INSERT; a trigger rejects
  UPDATE, DELETE and TRUNCATE for every role).
* ``workflow_runs``: one row per LangGraph run. ``thread_id`` must equal
  ``org_id || ':' || id`` (check constraint). UPDATE only on the lifecycle columns.
* ``approvals``, ``approval_steps``: UPDATE only on state columns. The decision
  ``permission``, ``payload_hash``, chain and assignees can never be changed by the app.
* ``approval_decisions``: append only (SELECT, INSERT, plus the immutability trigger).
* ``approval_delegations``: UPDATE only on ``revoked_at``.
* ``approval_links``: UPDATE only on ``used_at``, ``invalidated_at``, ``decision_id``,
  and a trigger refuses any change to a link that is already used or invalidated.

LangGraph checkpoints (``langgraph-checkpoint-postgres``) live in schema
``lg_checkpoints``: the package's tables (DDL of its migrations 0 to 9, applied here so
the app never runs ``setup()``), each with ``org_id uuid NOT NULL DEFAULT
app_current_org_id()``, RLS forced, the tenant policy, and a check that the thread id
starts with that org id. See ``pairly_core.workflow.checkpoint`` for the full design.

SECURITY DEFINER functions (owner role, flag-gated owner-only SELECT policies, the same
pattern as 0002 and 0003), each returning the least possible:

* ``approval_link_org_id(token_hash)``: the organisation of the link with that hash.
* ``workflow_resumable_org_ids()``: organisations with a run waiting for an approval
  that is already decided (the worker's reconciliation job).
* ``approval_overdue_org_ids()``: organisations with an overdue, not yet escalated step
  (the worker's SLA job).

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-07
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from pairly_core.migration_helpers import CURRENT_ORG_FN, app_db_role, org_rls_statements

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "lg_checkpoints"
CHECKPOINT_MIGRATIONS = 10  # versions 0..9 of langgraph-checkpoint-postgres 3.1
_HEX64 = "'^[0-9a-f]{64}$'"
_WORKFLOW_RE = "'^[a-z][a-z0-9_]{0,31}$'"
_UUID_ARRAY = postgresql.ARRAY(sa.Uuid())


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


def _ts(name: str, *, nullable: bool = True, default: bool = False) -> sa.Column[Any]:
    return sa.Column(
        name,
        sa.DateTime(timezone=True),
        nullable=nullable,
        server_default=sa.func.now() if default else None,
    )


def _user_fk(table: str, column: str, *, nullable: bool = False) -> sa.Column[Any]:
    return sa.Column(
        column,
        sa.Uuid(),
        sa.ForeignKey("users.id", ondelete="RESTRICT", name=f"fk_{table}_{column}_users"),
        nullable=nullable,
    )


def _immutable(table: str) -> None:
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

    op.execute(
        """
        CREATE FUNCTION workflow_reject_change() RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = pg_catalog, public
        AS $$
        BEGIN
            RAISE EXCEPTION '% is append-only: % is not allowed', TG_TABLE_NAME, TG_OP
                USING ERRCODE = 'insufficient_privilege';
        END
        $$
        """
    )
    op.execute("REVOKE ALL ON FUNCTION workflow_reject_change() FROM PUBLIC")

    # ---------------------------------------------------------------- policy_versions
    op.create_table(
        "policy_versions",
        _pk(),
        _org("policy_versions"),
        sa.Column("kind", sa.String(64), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("created_by_type", sa.String(16), nullable=False),
        sa.Column("created_by_id", sa.String(128), nullable=False),
        _ts("created_at", nullable=False, default=True),
        sa.UniqueConstraint("org_id", "kind", "name", "version", name="uq_policy_versions_version"),
        sa.CheckConstraint("version >= 1", name="ck_policy_versions_version_positive"),
        sa.CheckConstraint(
            f"content_hash ~ {_HEX64}", name="ck_policy_versions_content_hash_format"
        ),
        sa.CheckConstraint(
            "created_by_type IN ('user', 'agent', 'system')",
            name="ck_policy_versions_creator_valid",
        ),
    )
    op.create_index("ix_policy_versions_org_id", "policy_versions", ["org_id"])
    for stmt in org_rls_statements("policy_versions", app_role, privileges="SELECT, INSERT"):
        op.execute(stmt)
    _immutable("policy_versions")

    # ---------------------------------------------------------------- workflow_runs
    op.create_table(
        "workflow_runs",
        _pk(),
        _org("workflow_runs"),
        sa.Column("workflow", sa.String(32), nullable=False),
        sa.Column("graph_name", sa.String(64), nullable=False),
        sa.Column("graph_version", sa.Integer(), nullable=False),
        sa.Column("thread_id", sa.String(100), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("entity_type", sa.String(64), nullable=True),
        sa.Column("entity_id", sa.String(128), nullable=True),
        sa.Column("started_by_type", sa.String(16), nullable=False),
        sa.Column("started_by_id", sa.String(128), nullable=False),
        sa.Column("pending_approval_id", sa.Uuid(), nullable=True),
        sa.Column("error", sa.String(500), nullable=True),
        _ts("created_at", nullable=False, default=True),
        _ts("updated_at", nullable=False, default=True),
        _ts("claimed_at"),
        _ts("completed_at"),
        sa.UniqueConstraint("thread_id", name="uq_workflow_runs_thread_id"),
        sa.CheckConstraint(
            "status IN ('running', 'waiting_approval', 'completed', 'failed', 'cancelled')",
            name="ck_workflow_runs_status_valid",
        ),
        sa.CheckConstraint(
            "thread_id = org_id::text || ':' || id::text", name="ck_workflow_runs_thread_id_bound"
        ),
        sa.CheckConstraint(
            "started_by_type IN ('user', 'agent', 'system')",
            name="ck_workflow_runs_started_by_valid",
        ),
        sa.CheckConstraint(f"workflow ~ {_WORKFLOW_RE}", name="ck_workflow_runs_workflow_format"),
    )
    op.create_index("ix_workflow_runs_org_id", "workflow_runs", ["org_id"])
    op.create_index("ix_workflow_runs_org_status", "workflow_runs", ["org_id", "status"])
    for stmt in org_rls_statements("workflow_runs", app_role, privileges="SELECT, INSERT"):
        op.execute(stmt)
    op.execute(
        "GRANT UPDATE (status, pending_approval_id, error, updated_at, claimed_at, completed_at) "
        f"ON workflow_runs TO {app_role}"
    )

    # ---------------------------------------------------------------- approvals
    op.create_table(
        "approvals",
        _pk(),
        _org("approvals"),
        sa.Column("workflow", sa.String(32), nullable=False),
        sa.Column("approval_type", sa.String(64), nullable=False),
        sa.Column("permission", sa.String(128), nullable=False),
        sa.Column(
            "run_id",
            sa.Uuid(),
            sa.ForeignKey(
                "workflow_runs.id", ondelete="RESTRICT", name="fk_approvals_run_id_workflow_runs"
            ),
            nullable=False,
        ),
        sa.Column("step_key", sa.String(64), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("entity_type", sa.String(64), nullable=True),
        sa.Column("entity_id", sa.String(128), nullable=True),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("context", postgresql.JSONB(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("payload_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("submitted_by_type", sa.String(16), nullable=False),
        sa.Column("submitted_by_id", sa.String(128), nullable=False),
        sa.Column(
            "preparer_ids", _UUID_ARRAY, nullable=False, server_default=sa.text("'{}'::uuid[]")
        ),
        sa.Column(
            "policy_version_id",
            sa.Uuid(),
            sa.ForeignKey(
                "policy_versions.id",
                ondelete="RESTRICT",
                name="fk_approvals_policy_version_id_policy_versions",
            ),
            nullable=False,
        ),
        sa.Column("chain_name", sa.String(64), nullable=False),
        sa.Column("current_step", sa.Integer(), nullable=True),
        sa.Column("state_version", sa.Integer(), nullable=False),
        _ts("created_at", nullable=False, default=True),
        _ts("updated_at", nullable=False, default=True),
        _ts("decided_at"),
        _ts("executed_at"),
        sa.UniqueConstraint(
            "run_id", "step_key", "revision", name="uq_approvals_run_step_revision"
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'approved', 'rejected', 'changes_requested', 'cancelled')",
            name="ck_approvals_status_valid",
        ),
        sa.CheckConstraint(f"payload_hash ~ {_HEX64}", name="ck_approvals_payload_hash_format"),
        sa.CheckConstraint(
            "submitted_by_type IN ('user', 'agent', 'system')", name="ck_approvals_submitter_valid"
        ),
        sa.CheckConstraint("revision >= 1", name="ck_approvals_revision_positive"),
        sa.CheckConstraint(
            "permission ~ '^[a-z][a-z0-9_]{0,31}\\.[a-z][a-z0-9_]{0,63}\\.approve$'",
            name="ck_approvals_permission_is_approve",
        ),
        sa.CheckConstraint(
            "split_part(permission, '.', 1) = workflow", name="ck_approvals_permission_in_workflow"
        ),
        sa.CheckConstraint(
            "executed_at IS NULL OR status = 'approved'", name="ck_approvals_execute_only_approved"
        ),
    )
    op.create_index("ix_approvals_org_id", "approvals", ["org_id"])
    op.create_index("ix_approvals_run_id", "approvals", ["run_id"])
    op.create_index(
        "ix_approvals_org_inbox", "approvals", ["org_id", "permission", "created_at", "id"]
    )
    for stmt in org_rls_statements("approvals", app_role, privileges="SELECT, INSERT"):
        op.execute(stmt)
    op.execute(
        "GRANT UPDATE (status, current_step, state_version, updated_at, decided_at, "
        f"executed_at) ON approvals TO {app_role}"
    )

    # ---------------------------------------------------------------- approval_steps
    op.create_table(
        "approval_steps",
        _pk(),
        _org("approval_steps"),
        sa.Column(
            "approval_id",
            sa.Uuid(),
            sa.ForeignKey(
                "approvals.id", ondelete="CASCADE", name="fk_approval_steps_approval_id_approvals"
            ),
            nullable=False,
        ),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("mode", sa.String(16), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("assignee_ids", _UUID_ARRAY, nullable=False),
        sa.Column(
            "escalation_ids", _UUID_ARRAY, nullable=False, server_default=sa.text("'{}'::uuid[]")
        ),
        sa.Column("sla_seconds", sa.Integer(), nullable=False),
        sa.Column("warn_seconds", sa.Integer(), nullable=False),
        _ts("activated_at"),
        _ts("warn_at"),
        _ts("due_at"),
        _ts("escalated_at"),
        sa.Column("escalation_level", sa.Integer(), nullable=False, server_default="0"),
        _ts("completed_at"),
        sa.UniqueConstraint("approval_id", "position", name="uq_approval_steps_position"),
        sa.CheckConstraint("mode IN ('any_of', 'all_of')", name="ck_approval_steps_mode_valid"),
        sa.CheckConstraint(
            "status IN ('waiting', 'active', 'approved', 'rejected', 'changes_requested', "
            "'cancelled')",
            name="ck_approval_steps_status_valid",
        ),
        sa.CheckConstraint(
            "cardinality(assignee_ids) >= 1", name="ck_approval_steps_has_assignees"
        ),
    )
    op.create_index("ix_approval_steps_org_id", "approval_steps", ["org_id"])
    op.create_index("ix_approval_steps_approval_id", "approval_steps", ["approval_id"])
    op.create_index("ix_approval_steps_due", "approval_steps", ["status", "due_at"])
    for stmt in org_rls_statements("approval_steps", app_role, privileges="SELECT, INSERT"):
        op.execute(stmt)
    op.execute(
        "GRANT UPDATE (status, activated_at, warn_at, due_at, escalated_at, escalation_level, "
        f"escalation_ids, completed_at) ON approval_steps TO {app_role}"
    )

    # ---------------------------------------------------------------- approval_decisions
    op.create_table(
        "approval_decisions",
        _pk(),
        _org("approval_decisions"),
        sa.Column(
            "approval_id",
            sa.Uuid(),
            sa.ForeignKey(
                "approvals.id",
                ondelete="RESTRICT",
                name="fk_approval_decisions_approval_id_approvals",
            ),
            nullable=False,
        ),
        sa.Column(
            "step_id",
            sa.Uuid(),
            sa.ForeignKey(
                "approval_steps.id",
                ondelete="RESTRICT",
                name="fk_approval_decisions_step_id_approval_steps",
            ),
            nullable=False,
        ),
        _user_fk("approval_decisions", "actor_user_id"),
        _user_fk("approval_decisions", "on_behalf_of", nullable=True),
        sa.Column("via_escalation", sa.Boolean(), nullable=False),
        sa.Column("decision", sa.String(24), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("channel", sa.String(16), nullable=False),
        sa.Column("link_id", sa.Uuid(), nullable=True),
        sa.Column("permission", sa.String(128), nullable=False),
        sa.Column("second_factor", sa.Boolean(), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=True),
        sa.Column("request_hash", sa.String(64), nullable=False),
        _ts("created_at", nullable=False, default=True),
        sa.UniqueConstraint("step_id", "actor_user_id", name="uq_approval_decisions_step_actor"),
        sa.UniqueConstraint(
            "org_id", "actor_user_id", "idempotency_key", name="uq_approval_decisions_idempotency"
        ),
        sa.CheckConstraint(
            "decision IN ('approve', 'reject', 'request_changes')",
            name="ck_approval_decisions_decision_valid",
        ),
        sa.CheckConstraint(
            "channel IN ('web', 'email_link', 'slack_link')",
            name="ck_approval_decisions_channel_valid",
        ),
        sa.CheckConstraint(
            "decision = 'approve' OR (reason IS NOT NULL AND length(btrim(reason)) > 0)",
            name="ck_approval_decisions_reason_required",
        ),
        sa.CheckConstraint(
            f"request_hash ~ {_HEX64}", name="ck_approval_decisions_request_hash_format"
        ),
        sa.CheckConstraint(
            "on_behalf_of IS NULL OR on_behalf_of <> actor_user_id",
            name="ck_approval_decisions_behalf_of_other",
        ),
    )
    op.create_index("ix_approval_decisions_org_id", "approval_decisions", ["org_id"])
    op.create_index("ix_approval_decisions_approval_id", "approval_decisions", ["approval_id"])
    for stmt in org_rls_statements("approval_decisions", app_role, privileges="SELECT, INSERT"):
        op.execute(stmt)
    _immutable("approval_decisions")

    # ---------------------------------------------------------------- approval_delegations
    op.create_table(
        "approval_delegations",
        _pk(),
        _org("approval_delegations"),
        _user_fk("approval_delegations", "delegator_id"),
        _user_fk("approval_delegations", "delegate_id"),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("workflow", sa.String(32), nullable=True),
        _ts("starts_at", nullable=False),
        _ts("ends_at", nullable=False),
        sa.Column("reason", sa.String(500), nullable=True),
        _user_fk("approval_delegations", "created_by"),
        _ts("created_at", nullable=False, default=True),
        _ts("revoked_at"),
        sa.CheckConstraint("delegator_id <> delegate_id", name="ck_approval_delegations_not_self"),
        sa.CheckConstraint("ends_at > starts_at", name="ck_approval_delegations_range_valid"),
        sa.CheckConstraint(
            "kind IN ('delegation', 'out_of_office')", name="ck_approval_delegations_kind_valid"
        ),
        sa.CheckConstraint(
            f"workflow IS NULL OR workflow ~ {_WORKFLOW_RE}",
            name="ck_approval_delegations_workflow_format",
        ),
    )
    op.create_index("ix_approval_delegations_org_id", "approval_delegations", ["org_id"])
    op.create_index(
        "ix_approval_delegations_delegator_id", "approval_delegations", ["delegator_id"]
    )
    op.create_index("ix_approval_delegations_delegate_id", "approval_delegations", ["delegate_id"])
    for stmt in org_rls_statements("approval_delegations", app_role, privileges="SELECT, INSERT"):
        op.execute(stmt)
    op.execute(f"GRANT UPDATE (revoked_at) ON approval_delegations TO {app_role}")

    # ---------------------------------------------------------------- approval_links
    op.create_table(
        "approval_links",
        _pk(),
        _org("approval_links"),
        sa.Column(
            "approval_id",
            sa.Uuid(),
            sa.ForeignKey(
                "approvals.id", ondelete="RESTRICT", name="fk_approval_links_approval_id_approvals"
            ),
            nullable=False,
        ),
        sa.Column(
            "step_id",
            sa.Uuid(),
            sa.ForeignKey(
                "approval_steps.id",
                ondelete="RESTRICT",
                name="fk_approval_links_step_id_approval_steps",
            ),
            nullable=False,
        ),
        _user_fk("approval_links", "user_id"),
        sa.Column("action", sa.String(24), nullable=False),
        sa.Column("channel", sa.String(16), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("approval_state_version", sa.Integer(), nullable=False),
        _ts("created_at", nullable=False, default=True),
        _ts("expires_at", nullable=False),
        _ts("used_at"),
        _ts("invalidated_at"),
        sa.Column("decision_id", sa.Uuid(), nullable=True),
        sa.UniqueConstraint("token_hash", name="uq_approval_links_token_hash"),
        sa.CheckConstraint(f"token_hash ~ {_HEX64}", name="ck_approval_links_token_hash_format"),
        sa.CheckConstraint(
            "action IN ('approve', 'reject', 'request_changes')",
            name="ck_approval_links_action_valid",
        ),
        sa.CheckConstraint(
            "channel IN ('email_link', 'slack_link')", name="ck_approval_links_channel_valid"
        ),
        sa.CheckConstraint(
            "expires_at > created_at", name="ck_approval_links_expiry_after_creation"
        ),
        sa.CheckConstraint(
            "used_at IS NULL OR invalidated_at IS NULL", name="ck_approval_links_single_outcome"
        ),
        sa.CheckConstraint(
            "(used_at IS NULL) = (decision_id IS NULL)", name="ck_approval_links_used_pair"
        ),
    )
    op.create_index("ix_approval_links_org_id", "approval_links", ["org_id"])
    op.create_index("ix_approval_links_approval_id", "approval_links", ["approval_id"])
    for stmt in org_rls_statements("approval_links", app_role, privileges="SELECT, INSERT"):
        op.execute(stmt)
    op.execute(
        f"GRANT UPDATE (used_at, invalidated_at, decision_id) ON approval_links TO {app_role}"
    )
    op.execute(
        """
        CREATE FUNCTION approval_links_reject_terminal_change() RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = pg_catalog, public
        AS $$
        BEGIN
            IF OLD.used_at IS NOT NULL OR OLD.invalidated_at IS NOT NULL THEN
                RAISE EXCEPTION 'approval link is already used or invalidated'
                    USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER approval_links_terminal BEFORE UPDATE ON approval_links "
        "FOR EACH ROW EXECUTE FUNCTION approval_links_reject_terminal_change()"
    )
    op.execute("ALTER TABLE approval_links ENABLE ALWAYS TRIGGER approval_links_terminal")
    op.execute("REVOKE ALL ON FUNCTION approval_links_reject_terminal_change() FROM PUBLIC")

    # ---------------------------------------------------------------- SECURITY DEFINER
    op.execute(
        "CREATE POLICY approval_links_token_lookup ON approval_links FOR SELECT "
        "TO CURRENT_USER USING (current_setting('app.approval_link_lookup', true) = 'on')"
    )
    op.execute(
        """
        CREATE FUNCTION approval_link_org_id(p_token_hash text) RETURNS uuid
        LANGUAGE plpgsql VOLATILE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        DECLARE
            previous text := current_setting('app.approval_link_lookup', true);
            found uuid;
        BEGIN
            IF p_token_hash IS NULL OR p_token_hash !~ '^[0-9a-f]{64}$' THEN
                RETURN NULL;
            END IF;
            PERFORM set_config('app.approval_link_lookup', 'on', true);
            SELECT l.org_id INTO found
              FROM public.approval_links l
             WHERE l.token_hash = p_token_hash;
            PERFORM set_config('app.approval_link_lookup', coalesce(previous, ''), true);
            RETURN found;
        END
        $$
        """
    )
    op.execute("REVOKE ALL ON FUNCTION approval_link_org_id(text) FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION approval_link_org_id(text) TO {app_role}")

    for table in ("workflow_runs", "approvals", "approval_steps"):
        op.execute(
            f"CREATE POLICY {table}_worker_scan ON {table} FOR SELECT TO CURRENT_USER "
            "USING (current_setting('app.workflow_scan', true) = 'on')"
        )
    op.execute(
        """
        CREATE FUNCTION workflow_resumable_org_ids() RETURNS SETOF uuid
        LANGUAGE plpgsql VOLATILE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        DECLARE
            previous text := current_setting('app.workflow_scan', true);
            ids uuid[];
        BEGIN
            PERFORM set_config('app.workflow_scan', 'on', true);
            SELECT array_agg(DISTINCT r.org_id ORDER BY r.org_id) INTO ids
              FROM public.workflow_runs r
              JOIN public.approvals a ON a.id = r.pending_approval_id AND a.org_id = r.org_id
             WHERE r.status = 'waiting_approval'
               AND a.status IN ('approved', 'rejected', 'changes_requested');
            PERFORM set_config('app.workflow_scan', coalesce(previous, ''), true);
            RETURN QUERY SELECT unnest(coalesce(ids, '{}'::uuid[]));
        END
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION approval_overdue_org_ids() RETURNS SETOF uuid
        LANGUAGE plpgsql VOLATILE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        DECLARE
            previous text := current_setting('app.workflow_scan', true);
            ids uuid[];
        BEGIN
            PERFORM set_config('app.workflow_scan', 'on', true);
            SELECT array_agg(DISTINCT s.org_id ORDER BY s.org_id) INTO ids
              FROM public.approval_steps s
              JOIN public.approvals a ON a.id = s.approval_id AND a.org_id = s.org_id
             WHERE s.status = 'active'
               AND s.escalated_at IS NULL
               AND s.due_at < now()
               AND a.status = 'pending';
            PERFORM set_config('app.workflow_scan', coalesce(previous, ''), true);
            RETURN QUERY SELECT unnest(coalesce(ids, '{}'::uuid[]));
        END
        $$
        """
    )
    for fn in ("workflow_resumable_org_ids()", "approval_overdue_org_ids()"):
        op.execute(f"REVOKE ALL ON FUNCTION {fn} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION {fn} TO {app_role}")

    # ---------------------------------------------------------------- LangGraph checkpoints
    _checkpoint_schema(app_role)


def _checkpoint_schema(app_role: str) -> None:
    """The tables of langgraph-checkpoint-postgres (its migrations 0..9), tenant bound."""
    op.execute(f"CREATE SCHEMA {SCHEMA}")
    op.execute(f"REVOKE ALL ON SCHEMA {SCHEMA} FROM PUBLIC")
    op.execute(f"GRANT USAGE ON SCHEMA {SCHEMA} TO {app_role}")
    org_column = f"org_id uuid NOT NULL DEFAULT public.{CURRENT_ORG_FN}"
    thread_check = "CHECK (split_part(thread_id, ':', 1) = org_id::text)"
    op.execute(f"CREATE TABLE {SCHEMA}.checkpoint_migrations (v INTEGER PRIMARY KEY)")
    op.execute(
        f"""
        CREATE TABLE {SCHEMA}.checkpoints (
            thread_id TEXT NOT NULL,
            checkpoint_ns TEXT NOT NULL DEFAULT '',
            checkpoint_id TEXT NOT NULL,
            parent_checkpoint_id TEXT,
            type TEXT,
            checkpoint JSONB NOT NULL,
            metadata JSONB NOT NULL DEFAULT '{{}}',
            {org_column},
            CONSTRAINT ck_checkpoints_thread_org {thread_check},
            PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id)
        )
        """
    )
    op.execute(
        f"""
        CREATE TABLE {SCHEMA}.checkpoint_blobs (
            thread_id TEXT NOT NULL,
            checkpoint_ns TEXT NOT NULL DEFAULT '',
            channel TEXT NOT NULL,
            version TEXT NOT NULL,
            type TEXT NOT NULL,
            blob BYTEA,
            {org_column},
            CONSTRAINT ck_checkpoint_blobs_thread_org {thread_check},
            PRIMARY KEY (thread_id, checkpoint_ns, channel, version)
        )
        """
    )
    op.execute(
        f"""
        CREATE TABLE {SCHEMA}.checkpoint_writes (
            thread_id TEXT NOT NULL,
            checkpoint_ns TEXT NOT NULL DEFAULT '',
            checkpoint_id TEXT NOT NULL,
            task_id TEXT NOT NULL,
            idx INTEGER NOT NULL,
            channel TEXT NOT NULL,
            type TEXT,
            blob BYTEA NOT NULL,
            task_path TEXT NOT NULL DEFAULT '',
            {org_column},
            CONSTRAINT ck_checkpoint_writes_thread_org {thread_check},
            PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id, task_id, idx)
        )
        """
    )
    for table in ("checkpoints", "checkpoint_blobs", "checkpoint_writes"):
        qualified = f"{SCHEMA}.{table}"
        op.execute(f"CREATE INDEX {table}_thread_id_idx ON {qualified} (thread_id)")
        op.execute(f"CREATE INDEX {table}_org_id_idx ON {qualified} (org_id)")
        op.execute(f"ALTER TABLE {qualified} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {qualified} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY {table}_tenant_isolation ON {qualified} "
            f"USING (org_id = public.{CURRENT_ORG_FN}) "
            f"WITH CHECK (org_id = public.{CURRENT_ORG_FN})"
        )
        op.execute(f"REVOKE ALL ON {qualified} FROM PUBLIC")
        op.execute(f"REVOKE ALL ON {qualified} FROM {app_role}")
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {qualified} TO {app_role}")
    op.execute(
        f"INSERT INTO {SCHEMA}.checkpoint_migrations (v) "
        f"SELECT generate_series(0, {CHECKPOINT_MIGRATIONS - 1})"
    )
    op.execute(f"REVOKE ALL ON {SCHEMA}.checkpoint_migrations FROM PUBLIC")
    op.execute(f"GRANT SELECT ON {SCHEMA}.checkpoint_migrations TO {app_role}")


def downgrade() -> None:
    op.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
    op.execute("DROP FUNCTION IF EXISTS approval_overdue_org_ids()")
    op.execute("DROP FUNCTION IF EXISTS workflow_resumable_org_ids()")
    op.execute("DROP FUNCTION IF EXISTS approval_link_org_id(text)")
    op.drop_table("approval_links")
    op.execute("DROP FUNCTION IF EXISTS approval_links_reject_terminal_change()")
    op.drop_table("approval_delegations")
    op.drop_table("approval_decisions")
    op.drop_table("approval_steps")
    op.drop_table("approvals")
    op.drop_table("workflow_runs")
    op.drop_table("policy_versions")
    op.execute("DROP FUNCTION IF EXISTS workflow_reject_change()")
