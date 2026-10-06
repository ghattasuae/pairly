# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Audit log: append-only, hash-chained ``audit_events`` (WP0.5).

* Per-organisation chain: ``(org_id, seq)`` unique, ``seq`` from 1, ``prev_hash`` links
  to the previous event's ``hash`` (genesis: 64 zeros). Hashes are computed by
  ``pairly_core.audit.append_audit``; see ``canonical_event_json`` for the hashed bytes.
* Row Level Security forced, standard tenant policy. The application role gets
  INSERT and SELECT only (no UPDATE, DELETE or TRUNCATE privilege).
* ``audit_events_immutable`` (BEFORE UPDATE OR DELETE, per row) and
  ``audit_events_no_truncate`` (BEFORE TRUNCATE, per statement) raise for every role,
  the owner included. Both are ENABLE ALWAYS, so they also fire when
  ``session_replication_role = replica``.
* ``audit_events_link`` (BEFORE INSERT) rejects a row whose ``seq`` and ``prev_hash`` do
  not continue the current end of its chain. Defence in depth behind the advisory lock.
* ``audit_org_ids()``: SECURITY DEFINER, owned by the migration (owner) role, returns
  the distinct organisation ids that have audit events and nothing else. It is the only
  way the nightly verify job (running as the application role) can enumerate chains.
  Because RLS is forced on the owner too, the function turns ``app.audit_org_scan`` on
  for its own query only, and one extra SELECT policy, limited to the owner role, admits
  rows while that flag is on. The application role cannot use that policy (it applies
  to the owner role only) and has EXECUTE on the function alone.

``org_id`` has no foreign key on purpose: the audit trail outlives organisation rows.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-07
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from pairly_core.migration_helpers import app_db_role, org_rls_statements

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_HEX64 = "'^[0-9a-f]{64}$'"


def upgrade() -> None:
    app_role = app_db_role()

    op.create_table(
        "audit_events",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("seq", sa.BigInteger(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("actor_type", sa.String(16), nullable=False),
        sa.Column("actor_id", sa.String(128), nullable=True),
        sa.Column("action", sa.String(128), nullable=False),
        sa.Column("entity_type", sa.String(64), nullable=True),
        sa.Column("entity_id", sa.String(128), nullable=True),
        sa.Column("workflow", sa.String(32), nullable=True),
        sa.Column(
            "payload",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("request_id", sa.String(128), nullable=True),
        sa.Column("prev_hash", sa.String(64), nullable=False),
        sa.Column("hash", sa.String(64), nullable=False),
        sa.UniqueConstraint("org_id", "seq", name="uq_audit_events_org_id_seq"),
        sa.CheckConstraint("seq >= 1", name="ck_audit_events_seq_positive"),
        sa.CheckConstraint(
            "actor_type IN ('user', 'agent', 'system')", name="ck_audit_events_actor_type_valid"
        ),
        sa.CheckConstraint(f"prev_hash ~ {_HEX64}", name="ck_audit_events_prev_hash_format"),
        sa.CheckConstraint(f"hash ~ {_HEX64}", name="ck_audit_events_hash_format"),
        sa.CheckConstraint(
            "jsonb_typeof(payload) = 'object'", name="ck_audit_events_payload_object"
        ),
        sa.CheckConstraint(
            "(seq = 1) = (prev_hash = repeat('0', 64))", name="ck_audit_events_genesis"
        ),
    )
    op.create_index("ix_audit_events_org_id_occurred_at", "audit_events", ["org_id", "occurred_at"])
    op.create_index(
        "ix_audit_events_org_id_entity", "audit_events", ["org_id", "entity_type", "entity_id"]
    )

    for stmt in org_rls_statements("audit_events", app_role, privileges="SELECT, INSERT"):
        op.execute(stmt)

    # ---------------------------------------------------------------- immutability
    op.execute(
        """
        CREATE FUNCTION audit_events_reject_change() RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = pg_catalog, public
        AS $$
        BEGIN
            RAISE EXCEPTION 'audit_events is append-only: % is not allowed', TG_OP
                USING ERRCODE = 'insufficient_privilege';
        END
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER audit_events_immutable BEFORE UPDATE OR DELETE ON audit_events "
        "FOR EACH ROW EXECUTE FUNCTION audit_events_reject_change()"
    )
    op.execute(
        "CREATE TRIGGER audit_events_no_truncate BEFORE TRUNCATE ON audit_events "
        "FOR EACH STATEMENT EXECUTE FUNCTION audit_events_reject_change()"
    )

    # ---------------------------------------------------------------- chain link check
    # Runs as the inserting role, so RLS limits the lookup to the row's own org (the
    # insert's WITH CHECK already pins org_id to the current tenant).
    op.execute(
        """
        CREATE FUNCTION audit_events_check_link() RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = pg_catalog, public
        AS $$
        DECLARE
            last_seq bigint;
            last_hash text;
        BEGIN
            SELECT e.seq, e.hash INTO last_seq, last_hash
              FROM public.audit_events e
             WHERE e.org_id = NEW.org_id
             ORDER BY e.seq DESC
             LIMIT 1;
            IF last_seq IS NULL THEN
                IF NEW.seq <> 1 OR NEW.prev_hash <> repeat('0', 64) THEN
                    RAISE EXCEPTION 'audit chain: first event must be seq 1 with genesis hash'
                        USING ERRCODE = 'integrity_constraint_violation';
                END IF;
            ELSIF NEW.seq <> last_seq + 1 OR NEW.prev_hash <> last_hash THEN
                RAISE EXCEPTION 'audit chain: event does not continue the chain'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            RETURN NEW;
        END
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER audit_events_link BEFORE INSERT ON audit_events "
        "FOR EACH ROW EXECUTE FUNCTION audit_events_check_link()"
    )
    for trigger in ("audit_events_immutable", "audit_events_no_truncate", "audit_events_link"):
        op.execute(f"ALTER TABLE audit_events ENABLE ALWAYS TRIGGER {trigger}")
    for fn in ("audit_events_reject_change()", "audit_events_check_link()"):
        op.execute(f"REVOKE ALL ON FUNCTION {fn} FROM PUBLIC")

    # ---------------------------------------------------------------- org enumeration
    op.execute(
        "CREATE POLICY audit_events_org_scan ON audit_events FOR SELECT TO CURRENT_USER "
        "USING (current_setting('app.audit_org_scan', true) = 'on')"
    )
    # The flag is set with set_config (a function-level SET of a custom parameter needs
    # superuser) and put back before returning. An error rolls the change back with the
    # statement, and the policy above ignores the flag for every role but the owner.
    op.execute(
        """
        CREATE FUNCTION audit_org_ids() RETURNS SETOF uuid
        LANGUAGE plpgsql VOLATILE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        DECLARE
            previous text := current_setting('app.audit_org_scan', true);
            ids uuid[];
        BEGIN
            PERFORM set_config('app.audit_org_scan', 'on', true);
            SELECT array_agg(DISTINCT e.org_id ORDER BY e.org_id) INTO ids
              FROM public.audit_events e;
            PERFORM set_config('app.audit_org_scan', coalesce(previous, ''), true);
            RETURN QUERY SELECT unnest(coalesce(ids, '{}'::uuid[]));
        END
        $$
        """
    )
    op.execute("REVOKE ALL ON FUNCTION audit_org_ids() FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION audit_org_ids() TO {app_role}")


def downgrade() -> None:
    # Dropping the table is the only way to remove audit events; the triggers go with it.
    op.execute("DROP FUNCTION IF EXISTS audit_org_ids()")
    op.drop_table("audit_events")
    op.execute("DROP FUNCTION IF EXISTS audit_events_check_link()")
    op.execute("DROP FUNCTION IF EXISTS audit_events_reject_change()")
