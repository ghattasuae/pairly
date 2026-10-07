# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""API keys, idempotency keys, webhooks; partial embedding billing; policy override.

WP1.11 (all new tables are org scoped with the standard RLS policy, forced):

* ``organisations.sandbox`` (bool, default false): test API keys only work there. The
  application role cannot change it (its UPDATE grant stays name and slug).
* ``audit_events.actor_type`` also accepts ``api_key``.
* ``api_keys``: SELECT, INSERT, UPDATE (``last_used_at``, ``revoked_at``,
  ``revoked_by``); a trigger makes a revocation final. ``api_key_org_id(prefix)``
  (SECURITY DEFINER, flag-gated owner-only SELECT policy, the 0006 pattern) returns only
  the organisation of a key prefix, for authentication before the tenant is known.
* ``idempotency_keys``: SELECT, INSERT, UPDATE, DELETE (rows expire after 24 hours).
* ``webhook_endpoints`` (SELECT, INSERT, UPDATE), ``webhook_outbox`` (SELECT, INSERT,
  UPDATE of ``dispatched_at``, DELETE), ``webhook_deliveries`` (SELECT, INSERT, UPDATE,
  DELETE), ``inbound_events`` (SELECT, INSERT, DELETE). ``webhook_due_org_ids()``
  (SECURITY DEFINER) returns the organisations with undispatched events or due
  deliveries, for the worker.

Fixups:

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

import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from pairly_core.migration_helpers import app_db_role, ident, org_rls_statements

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
    op.drop_constraint("ck_policy_documents_override_complete", "policy_documents", type_="check")
    op.drop_constraint(
        "ck_policy_documents_override_request_complete", "policy_documents", type_="check"
    )
    for prefix in ("override_requested", "overridden"):
        op.drop_constraint(
            f"fk_policy_documents_{prefix}_by_users", "policy_documents", type_="foreignkey"
        )
    for column in _OVERRIDE_COLUMNS:
        op.drop_column("policy_documents", column)


# ---------------------------------------------------------------- WP1.11

_ACTOR_TYPES_OLD = "actor_type IN ('user', 'agent', 'system')"
_ACTOR_TYPES_NEW = "actor_type IN ('user', 'agent', 'system', 'api_key')"


def _uuid_pk() -> sa.Column[uuid.UUID]:
    return sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()"))


def _org_fk(table: str) -> sa.Column[uuid.UUID]:
    return sa.Column(
        "org_id",
        sa.Uuid(),
        sa.ForeignKey(
            "organisations.id", ondelete="RESTRICT", name=f"fk_{table}_org_id_organisations"
        ),
        nullable=False,
    )


def _now() -> sa.TextClause:
    return sa.text("now()")


def _rls(table: str, role: str, privileges: str) -> None:
    for statement in org_rls_statements(table, role, privileges=privileges):
        op.execute(statement)


def _upgrade_api_keys(role: str) -> None:
    op.add_column(
        "organisations",
        sa.Column("sandbox", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.drop_constraint("ck_audit_events_actor_type_valid", "audit_events", type_="check")
    op.create_check_constraint("ck_audit_events_actor_type_valid", "audit_events", _ACTOR_TYPES_NEW)

    op.create_table(
        "api_keys",
        _uuid_pk(),
        _org_fk("api_keys"),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("prefix", sa.String(16), nullable=False),
        sa.Column("mode", sa.String(8), nullable=False),
        sa.Column("secret_hash", sa.String(64), nullable=False),
        sa.Column("scopes", postgresql.ARRAY(sa.String(100)), nullable=False),
        sa.Column(
            "created_by",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="RESTRICT", name="fk_api_keys_created_by_users"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "revoked_by",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="RESTRICT", name="fk_api_keys_revoked_by_users"),
            nullable=True,
        ),
        sa.Column("rate_limit_per_minute", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=_now()),
        sa.UniqueConstraint("prefix", name="uq_api_keys_prefix"),
        sa.CheckConstraint(
            "prefix ~ '^pk_(live|test)_[a-z0-9]{8}$'", name="ck_api_keys_prefix_format"
        ),
        sa.CheckConstraint("mode IN ('live', 'test')", name="ck_api_keys_mode_valid"),
        sa.CheckConstraint(
            "split_part(prefix, '_', 2) = mode", name="ck_api_keys_prefix_matches_mode"
        ),
        sa.CheckConstraint("secret_hash ~ '^[0-9a-f]{64}$'", name="ck_api_keys_secret_hash_format"),
        sa.CheckConstraint("char_length(name) BETWEEN 1 AND 100", name="ck_api_keys_name_length"),
        sa.CheckConstraint("expires_at > created_at", name="ck_api_keys_expiry_after_creation"),
        sa.CheckConstraint(
            "(revoked_at IS NULL) = (revoked_by IS NULL)", name="ck_api_keys_revoked_pair"
        ),
        sa.CheckConstraint(
            "rate_limit_per_minute IS NULL OR rate_limit_per_minute BETWEEN 1 AND 100000",
            name="ck_api_keys_rate_limit_range",
        ),
    )
    op.create_index("ix_api_keys_org_id", "api_keys", ["org_id"])
    _rls("api_keys", role, "SELECT, INSERT")
    op.execute(f"GRANT UPDATE (last_used_at, revoked_at, revoked_by) ON api_keys TO {role}")
    op.execute(
        """
        CREATE FUNCTION api_keys_guard() RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = pg_catalog, public
        AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'api_keys rows cannot be deleted'
                    USING ERRCODE = 'insufficient_privilege';
            END IF;
            IF OLD.revoked_at IS NOT NULL
               AND (NEW.revoked_at IS DISTINCT FROM OLD.revoked_at
                    OR NEW.revoked_by IS DISTINCT FROM OLD.revoked_by) THEN
                RAISE EXCEPTION 'a revoked API key stays revoked'
                    USING ERRCODE = 'insufficient_privilege';
            END IF;
            RETURN NEW;
        END
        $$
        """
    )
    op.execute("REVOKE ALL ON FUNCTION api_keys_guard() FROM PUBLIC")
    op.execute(
        "CREATE TRIGGER api_keys_guard BEFORE UPDATE OR DELETE ON api_keys "
        "FOR EACH ROW EXECUTE FUNCTION api_keys_guard()"
    )
    op.execute("ALTER TABLE api_keys ENABLE ALWAYS TRIGGER api_keys_guard")
    op.execute(
        "CREATE POLICY api_keys_prefix_lookup ON api_keys FOR SELECT TO CURRENT_USER "
        "USING (current_setting('app.api_key_lookup', true) = 'on')"
    )
    op.execute(
        """
        CREATE FUNCTION api_key_org_id(p_prefix text) RETURNS uuid
        LANGUAGE plpgsql VOLATILE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        DECLARE
            previous text := current_setting('app.api_key_lookup', true);
            found uuid;
        BEGIN
            IF p_prefix IS NULL OR p_prefix !~ '^pk_(live|test)_[a-z0-9]{8}$' THEN
                RETURN NULL;
            END IF;
            PERFORM set_config('app.api_key_lookup', 'on', true);
            SELECT k.org_id INTO found FROM public.api_keys k WHERE k.prefix = p_prefix;
            PERFORM set_config('app.api_key_lookup', coalesce(previous, ''), true);
            RETURN found;
        END
        $$
        """
    )
    op.execute("REVOKE ALL ON FUNCTION api_key_org_id(text) FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION api_key_org_id(text) TO {role}")

    op.create_table(
        "idempotency_keys",
        _uuid_pk(),
        _org_fk("idempotency_keys"),
        sa.Column("actor_type", sa.String(16), nullable=False),
        sa.Column("actor_id", sa.String(128), nullable=False),
        sa.Column("key", sa.String(128), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=True),
        sa.Column("response_status", sa.SmallInteger(), nullable=True),
        sa.Column("response_headers", postgresql.JSONB(), nullable=True),
        sa.Column("response_body", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=_now()),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "org_id", "actor_type", "actor_id", "key", name="uq_idempotency_keys_actor_key"
        ),
    )
    op.create_index("ix_idempotency_keys_org_id", "idempotency_keys", ["org_id"])
    op.create_index("ix_idempotency_keys_expires_at", "idempotency_keys", ["expires_at"])
    _rls("idempotency_keys", role, "SELECT, INSERT, UPDATE, DELETE")


def _downgrade_api_keys() -> None:
    op.drop_table("idempotency_keys")
    op.execute("DROP FUNCTION IF EXISTS api_key_org_id(text)")
    op.drop_table("api_keys")
    op.execute("DROP FUNCTION IF EXISTS api_keys_guard()")
    # Audit events are immutable, so api_key events cannot be rewritten: the old rule
    # comes back NOT VALID (it applies to new rows only).
    op.drop_constraint("ck_audit_events_actor_type_valid", "audit_events", type_="check")
    op.execute(
        "ALTER TABLE audit_events ADD CONSTRAINT ck_audit_events_ck_audit_events_actor_type_valid "
        f"CHECK ({_ACTOR_TYPES_OLD}) NOT VALID"
    )
    op.drop_column("organisations", "sandbox")


def _upgrade_webhooks(role: str) -> None:
    event_type_check = "~ '^[a-z][a-z0-9_]*([.][a-z][a-z0-9_]*){1,4}$'"
    op.create_table(
        "webhook_endpoints",
        _uuid_pk(),
        _org_fk("webhook_endpoints"),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("description", sa.String(200), nullable=True),
        sa.Column("secret", sa.Text(), nullable=False),
        sa.Column("event_types", postgresql.ARRAY(sa.String(100)), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("consecutive_failures", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("disabled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("disabled_reason", sa.String(64), nullable=True),
        sa.Column(
            "created_by",
            sa.Uuid(),
            sa.ForeignKey(
                "users.id", ondelete="RESTRICT", name="fk_webhook_endpoints_created_by_users"
            ),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=_now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=_now()),
        sa.CheckConstraint(
            "char_length(url) BETWEEN 8 AND 2048", name="ck_webhook_endpoints_url_length"
        ),
        sa.CheckConstraint(
            "cardinality(event_types) BETWEEN 1 AND 100",
            name="ck_webhook_endpoints_event_types_count",
        ),
        sa.CheckConstraint(
            "consecutive_failures >= 0", name="ck_webhook_endpoints_failures_non_negative"
        ),
        sa.CheckConstraint(
            "active OR disabled_at IS NOT NULL", name="ck_webhook_endpoints_inactive_has_time"
        ),
    )
    op.create_index("ix_webhook_endpoints_org_id", "webhook_endpoints", ["org_id"])
    _rls("webhook_endpoints", role, "SELECT, INSERT, UPDATE")

    op.create_table(
        "webhook_outbox",
        _uuid_pk(),
        _org_fk("webhook_outbox"),
        sa.Column("event_type", sa.String(100), nullable=False),
        sa.Column("data", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=_now()),
        sa.Column("dispatched_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            f"event_type {event_type_check}", name="ck_webhook_outbox_event_type_format"
        ),
        sa.CheckConstraint("jsonb_typeof(data) = 'object'", name="ck_webhook_outbox_data_object"),
    )
    op.create_index("ix_webhook_outbox_org_id", "webhook_outbox", ["org_id"])
    op.create_index(
        "ix_webhook_outbox_pending",
        "webhook_outbox",
        ["org_id", "created_at"],
        postgresql_where=sa.text("dispatched_at IS NULL"),
    )
    _rls("webhook_outbox", role, "SELECT, INSERT, DELETE")
    op.execute(f"GRANT UPDATE (dispatched_at) ON webhook_outbox TO {role}")

    op.create_table(
        "webhook_deliveries",
        _uuid_pk(),
        _org_fk("webhook_deliveries"),
        sa.Column(
            "endpoint_id",
            sa.Uuid(),
            sa.ForeignKey(
                "webhook_endpoints.id",
                ondelete="RESTRICT",
                name="fk_webhook_deliveries_endpoint_id_webhook_endpoints",
            ),
            nullable=False,
        ),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("event_type", sa.String(100), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "next_attempt_at", sa.DateTime(timezone=True), nullable=False, server_default=_now()
        ),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("response_code", sa.Integer(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("last_error", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=_now()),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("endpoint_id", "event_id", name="uq_webhook_deliveries_endpoint_event"),
        sa.CheckConstraint(
            "status IN ('pending', 'succeeded', 'failed', 'cancelled')",
            name="ck_webhook_deliveries_status_valid",
        ),
        sa.CheckConstraint("attempts >= 0", name="ck_webhook_deliveries_attempts_non_negative"),
        sa.CheckConstraint(
            "jsonb_typeof(payload) = 'object'", name="ck_webhook_deliveries_payload_object"
        ),
    )
    op.create_index("ix_webhook_deliveries_org_id", "webhook_deliveries", ["org_id"])
    op.create_index(
        "ix_webhook_deliveries_due",
        "webhook_deliveries",
        ["org_id", "next_attempt_at"],
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.create_index(
        "ix_webhook_deliveries_endpoint",
        "webhook_deliveries",
        ["org_id", "endpoint_id", "created_at"],
    )
    _rls("webhook_deliveries", role, "SELECT, INSERT, UPDATE, DELETE")

    op.create_table(
        "inbound_events",
        _uuid_pk(),
        _org_fk("inbound_events"),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("event_id", sa.String(255), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False, server_default=_now()),
        sa.UniqueConstraint(
            "org_id", "provider", "event_id", name="uq_inbound_events_provider_event"
        ),
        sa.CheckConstraint(
            "provider ~ '^[a-z][a-z0-9_]{0,31}$'", name="ck_inbound_events_provider_format"
        ),
        sa.CheckConstraint(
            "char_length(event_id) BETWEEN 1 AND 255", name="ck_inbound_events_event_id_length"
        ),
    )
    op.create_index("ix_inbound_events_org_id", "inbound_events", ["org_id"])
    _rls("inbound_events", role, "SELECT, INSERT, DELETE")

    for table in ("webhook_outbox", "webhook_deliveries"):
        op.execute(
            f"CREATE POLICY {table}_worker_scan ON {table} FOR SELECT TO CURRENT_USER "
            "USING (current_setting('app.webhook_scan', true) = 'on')"
        )
    op.execute(
        """
        CREATE FUNCTION webhook_due_org_ids() RETURNS SETOF uuid
        LANGUAGE plpgsql VOLATILE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        DECLARE
            previous text := current_setting('app.webhook_scan', true);
            ids uuid[];
        BEGIN
            PERFORM set_config('app.webhook_scan', 'on', true);
            SELECT array_agg(DISTINCT x.org_id ORDER BY x.org_id) INTO ids FROM (
                SELECT o.org_id FROM public.webhook_outbox o WHERE o.dispatched_at IS NULL
                UNION
                SELECT d.org_id FROM public.webhook_deliveries d
                 WHERE d.status = 'pending' AND d.next_attempt_at <= now()
                   AND (d.locked_until IS NULL OR d.locked_until < now())
            ) x;
            PERFORM set_config('app.webhook_scan', coalesce(previous, ''), true);
            RETURN QUERY SELECT unnest(coalesce(ids, '{}'::uuid[]));
        END
        $$
        """
    )
    op.execute("REVOKE ALL ON FUNCTION webhook_due_org_ids() FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION webhook_due_org_ids() TO {role}")


def _downgrade_webhooks() -> None:
    op.execute("DROP FUNCTION IF EXISTS webhook_due_org_ids()")
    for table in ("inbound_events", "webhook_deliveries", "webhook_outbox", "webhook_endpoints"):
        op.drop_table(table)


def upgrade() -> None:
    role = ident(app_db_role())
    _upgrade_model_calls()
    _upgrade_policy_override(role)
    _upgrade_api_keys(role)
    _upgrade_webhooks(role)


def downgrade() -> None:
    role = ident(app_db_role())
    _downgrade_webhooks()
    _downgrade_api_keys()
    _downgrade_policy_override(role)
    _downgrade_model_calls()
