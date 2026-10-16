# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Erasing a person's data from the append-only tables, and nothing else.

A workflow erases a person's personal data on a person's request (Pairly Hire:
``pairly_hire.erasure``). The audit and decision trail keeps ids, hashes, timestamps and
outcomes; everything that names or describes the person goes. The tables below were
append-only for the application role, so each gets the narrowest way to clear its
personal columns, always with a timestamp that says it was erased:

* ``documents``: status ``erased`` (the bytes are deleted from the object store after
  the erasure commits), ``erased_at`` and ``objects_erased_at`` (when the stored
  objects were confirmed gone). ``sha256`` and ``size_bytes`` stay for the trail.
* ``decision_records``: ``sensitive_erased_at``. ``decision_records_guard`` also admits
  one UPDATE that sets ``sensitive`` to NULL and ``sensitive_erased_at`` from NULL,
  every other column unchanged; the key rotation re-wrap stays for the owner role only
  (the application role gets UPDATE on these two columns for the erasure). The content
  hash and the audit chain stay, so verification shows the record existed and was
  erased (``verify_record``).
* ``model_calls``: ``erased_at``. The application role may UPDATE ``request``,
  ``response`` and ``erased_at``, only to erase (``model_calls_erasure_guard``:
  ``erased_at`` set from NULL, ``response`` NULL, every other column unchanged). Hashes,
  tokens and cost stay.
* ``notifications``: ``erased_at``. The application role may clear ``content`` and the
  recipient address the same way (``notifications_erasure_guard``).
* ``approvals``: ``erased_at``. The application role may replace ``context`` and
  ``payload`` by ``{"erased": true}``, once (``approvals_erasure_guard``).
* ``appeals``: ``reason_erased_at``. ``appeals_guard`` admits replacing the requester's
  reason once, also on a resolved appeal.
* ``extraction_fields`` and ``extraction_corrections``: still no UPDATE privilege. The
  SECURITY DEFINER function ``erase_extraction_values(uuid[])`` clears the values,
  raw values, spans and correction values of the given extractions of the current
  organisation, and the new guards admit exactly that change.

Revision ID: 0019
Revises: 0018
Create Date: 2026-10-10
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op

from pairly_core.migration_helpers import app_db_role, ident

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_STATUSES = "'received', 'scanning', 'clean', 'infected', 'rejected', 'parsed', 'parse_failed'"
_NEW_STATUSES = f"{_OLD_STATUSES}, 'erased'"
ERASED_TEXT = "[erased]"


def _ts(name: str) -> sa.Column[Any]:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=True)


def _guard(table: str, body: str, *, app_role: str | None = None) -> None:
    """A BEFORE UPDATE row trigger. With ``app_role`` it only checks that role."""
    who = f"current_user <> '{ident(app_role)}' OR " if app_role else ""
    op.execute(
        f"""
        CREATE FUNCTION {table}_erasure_guard() RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = pg_catalog, public
        AS $$
        BEGIN
            IF {who}({body}) THEN
                RETURN NEW;
            END IF;
            RAISE EXCEPTION '% is append-only: only an erasure may change it', TG_TABLE_NAME
                USING ERRCODE = 'insufficient_privilege';
        END
        $$
        """
    )
    op.execute(f"REVOKE ALL ON FUNCTION {table}_erasure_guard() FROM PUBLIC")
    op.execute(
        f"CREATE TRIGGER {table}_erasure_guard BEFORE UPDATE ON {table} "
        f"FOR EACH ROW EXECUTE FUNCTION {table}_erasure_guard()"
    )
    op.execute(f"ALTER TABLE {table} ENABLE ALWAYS TRIGGER {table}_erasure_guard")


def _same_except(*columns: str) -> str:
    minus = "".join(f" - '{c}'" for c in columns)
    return f"(to_jsonb(NEW){minus}) = (to_jsonb(OLD){minus})"


def _documents() -> None:
    op.drop_constraint("ck_documents_status_valid", "documents", type_="check")
    op.create_check_constraint(
        "ck_documents_status_valid", "documents", f"status IN ({_NEW_STATUSES})"
    )
    op.add_column("documents", _ts("erased_at"))
    op.add_column("documents", _ts("objects_erased_at"))


def _decision_records(app_role: str) -> None:
    op.add_column("decision_records", _ts("sensitive_erased_at"))
    role = ident(app_role)
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION decision_records_guard() RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = pg_catalog, public
        AS $$
        BEGIN
            IF TG_OP = 'UPDATE'
               AND current_user <> '{role}'
               AND (to_jsonb(NEW) - 'sensitive') = (to_jsonb(OLD) - 'sensitive')
               AND (NEW.sensitive IS NULL) = (OLD.sensitive IS NULL) THEN
                -- Key rotation (owner role) re-wraps the encrypted column and nothing
                -- else. The application role may only erase (below).
                RETURN NEW;
            END IF;
            IF TG_OP = 'UPDATE'
               AND (to_jsonb(NEW) - 'sensitive' - 'sensitive_erased_at')
                   = (to_jsonb(OLD) - 'sensitive' - 'sensitive_erased_at')
               AND OLD.sensitive_erased_at IS NULL
               AND NEW.sensitive_erased_at IS NOT NULL
               AND NEW.sensitive IS NULL THEN
                -- Erasure of the person's data: the personal part goes, once.
                RETURN NEW;
            END IF;
            RAISE EXCEPTION 'decision_records is append-only: % is not allowed', TG_OP
                USING ERRCODE = 'insufficient_privilege';
        END
        $$
        """
    )
    op.execute(f"GRANT UPDATE (sensitive, sensitive_erased_at) ON decision_records TO {app_role}")


def _model_calls(app_role: str) -> None:
    op.add_column("model_calls", _ts("erased_at"))
    op.execute(f"GRANT UPDATE (request, response, erased_at) ON model_calls TO {app_role}")
    _guard(
        "model_calls",
        f"{_same_except('request', 'response', 'erased_at')} "
        "AND OLD.erased_at IS NULL AND NEW.erased_at IS NOT NULL AND NEW.response IS NULL",
        app_role=app_role,
    )


def _notifications(app_role: str) -> None:
    op.add_column("notifications", _ts("erased_at"))
    op.execute(
        "GRANT UPDATE (content, recipient_address, recipient_bidx, erased_at) "
        f"ON notifications TO {app_role}"
    )
    erased = (
        f"{_same_except('content', 'recipient_address', 'recipient_bidx', 'erased_at', 'status', 'reason', 'next_attempt_at')} "  # noqa: E501
        "AND OLD.erased_at IS NULL AND NEW.erased_at IS NOT NULL AND NEW.content IS NULL"
    )
    untouched = (
        "NEW.content IS NOT DISTINCT FROM OLD.content "
        "AND NEW.recipient_address IS NOT DISTINCT FROM OLD.recipient_address "
        "AND NEW.recipient_bidx IS NOT DISTINCT FROM OLD.recipient_bidx "
        "AND NEW.erased_at IS NOT DISTINCT FROM OLD.erased_at"
    )
    # Delivery keeps updating its own columns; the personal ones change only to erase.
    _guard("notifications", f"({untouched}) OR ({erased})", app_role=app_role)


def _approvals(app_role: str) -> None:
    op.add_column("approvals", _ts("erased_at"))
    op.execute(f"GRANT UPDATE (context, payload, erased_at) ON approvals TO {app_role}")
    erased = (
        f"{_same_except('context', 'payload', 'erased_at')} "
        "AND OLD.erased_at IS NULL AND NEW.erased_at IS NOT NULL "
        """AND NEW.context = '{"erased": true}'::jsonb """
        """AND NEW.payload = '{"erased": true}'::jsonb"""
    )
    untouched = (
        "NEW.context = OLD.context AND NEW.payload = OLD.payload "
        "AND NEW.erased_at IS NOT DISTINCT FROM OLD.erased_at"
    )
    _guard("approvals", f"({untouched}) OR ({erased})", app_role=app_role)


def _appeals(app_role: str) -> None:
    op.add_column("appeals", _ts("reason_erased_at"))
    op.execute(f"GRANT UPDATE (reason, reason_erased_at) ON appeals TO {app_role}")
    role = ident(app_role)
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION appeals_guard() RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = pg_catalog, public
        AS $$
        DECLARE
            erasing boolean;
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'appeals rows cannot be deleted'
                    USING ERRCODE = 'insufficient_privilege';
            END IF;
            -- Erasure of the requester's words: the reason is replaced once, nothing else.
            erasing := (to_jsonb(NEW) - 'reason' - 'reason_erased_at')
                       = (to_jsonb(OLD) - 'reason' - 'reason_erased_at')
                       AND OLD.reason_erased_at IS NULL
                       AND NEW.reason_erased_at IS NOT NULL;
            IF NEW.reason_erased_at IS DISTINCT FROM OLD.reason_erased_at AND NOT erasing THEN
                RAISE EXCEPTION 'an erased appeal reason cannot change'
                    USING ERRCODE = 'insufficient_privilege';
            END IF;
            IF NOT erasing AND OLD.reason_erased_at IS NOT NULL
               AND NEW.reason IS DISTINCT FROM OLD.reason AND current_user = '{role}' THEN
                RAISE EXCEPTION 'an erased appeal reason cannot change'
                    USING ERRCODE = 'insufficient_privilege';
            END IF;
            IF NOT erasing AND OLD.status = 'resolved' AND current_user = '{role}' THEN
                RAISE EXCEPTION 'a resolved appeal cannot change'
                    USING ERRCODE = 'insufficient_privilege';
            END IF;
            IF OLD.status = 'resolved'
               AND (to_jsonb(NEW) - 'reason' - 'outcome_text' - 'reason_erased_at')
                   <> (to_jsonb(OLD) - 'reason' - 'outcome_text' - 'reason_erased_at') THEN
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


def _extractions(app_role: str) -> None:
    fields_erased = (
        f"{_same_except('value', 'raw_value', 'spans')} "
        "AND NEW.value IS NULL AND NEW.raw_value IS NULL AND NEW.spans = '[]'::jsonb"
    )
    corrections_erased = (
        f"{_same_except('old_value', 'new_value', 'reason')} "
        f"AND NEW.old_value IS NULL AND NEW.new_value IS NULL AND NEW.reason = '{ERASED_TEXT}'"
    )
    for table, erased in (
        ("extraction_fields", fields_erased),
        ("extraction_corrections", corrections_erased),
    ):
        # The old trigger refused UPDATE and DELETE; DELETE stays refused, UPDATE may
        # only be the erasure below (the application role still has no UPDATE grant).
        op.execute(f"DROP TRIGGER {table}_immutable ON {table}")
        op.execute(
            f"CREATE TRIGGER {table}_immutable BEFORE DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION workflow_reject_change()"
        )
        op.execute(f"ALTER TABLE {table} ENABLE ALWAYS TRIGGER {table}_immutable")
        _guard(table, erased)
    # Plain SQL (no interpolation): the tenant function and the marker text are fixed.
    op.execute(
        """
        CREATE FUNCTION erase_extraction_values(p_extraction_ids uuid[]) RETURNS integer
        LANGUAGE plpgsql VOLATILE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        DECLARE
            org uuid := public.app_current_org_id();
            changed integer := 0;
            n integer;
        BEGIN
            IF org IS NULL THEN
                RAISE EXCEPTION 'erase_extraction_values needs a tenant context'
                    USING ERRCODE = 'insufficient_privilege';
            END IF;
            UPDATE public.extraction_fields
               SET value = NULL, raw_value = NULL, spans = '[]'::jsonb
             WHERE org_id = org AND extraction_id = ANY(p_extraction_ids)
               AND (value IS NOT NULL OR raw_value IS NOT NULL OR spans <> '[]'::jsonb);
            GET DIAGNOSTICS n = ROW_COUNT;
            changed := changed + n;
            UPDATE public.extraction_corrections
               SET old_value = NULL, new_value = NULL, reason = '[erased]'
             WHERE org_id = org AND extraction_id = ANY(p_extraction_ids)
               AND reason <> '[erased]';
            GET DIAGNOSTICS n = ROW_COUNT;
            RETURN changed + n;
        END
        $$
        """
    )
    op.execute("REVOKE ALL ON FUNCTION erase_extraction_values(uuid[]) FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION erase_extraction_values(uuid[]) TO {app_role}")


def upgrade() -> None:
    app_role = app_db_role()
    _documents()
    _decision_records(app_role)
    _model_calls(app_role)
    _notifications(app_role)
    _approvals(app_role)
    _appeals(app_role)
    _extractions(app_role)


_OLD_DECISION_GUARD = """
    CREATE OR REPLACE FUNCTION decision_records_guard() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path = pg_catalog, public
    AS $$
    BEGIN
        IF TG_OP = 'UPDATE'
           AND (to_jsonb(NEW) - 'sensitive') = (to_jsonb(OLD) - 'sensitive')
           AND (NEW.sensitive IS NULL) = (OLD.sensitive IS NULL) THEN
            RETURN NEW;
        END IF;
        RAISE EXCEPTION 'decision_records is append-only: % is not allowed', TG_OP
            USING ERRCODE = 'insufficient_privilege';
    END
    $$
"""

_OLD_APPEALS_GUARD = """
    CREATE OR REPLACE FUNCTION appeals_guard() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path = pg_catalog, public
    AS $$
    BEGIN
        IF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION 'appeals rows cannot be deleted'
                USING ERRCODE = 'insufficient_privilege';
        END IF;
        IF OLD.status = 'resolved' AND current_user = '{role}' THEN
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


def downgrade() -> None:
    app_role = app_db_role()
    op.execute(_OLD_DECISION_GUARD)
    op.execute(_OLD_APPEALS_GUARD.format(role=ident(app_role)))
    op.execute("DROP FUNCTION IF EXISTS erase_extraction_values(uuid[])")
    for table in ("extraction_fields", "extraction_corrections"):
        op.execute(f"DROP TRIGGER IF EXISTS {table}_erasure_guard ON {table}")
        op.execute(f"DROP FUNCTION IF EXISTS {table}_erasure_guard()")
        op.execute(f"DROP TRIGGER {table}_immutable ON {table}")
        op.execute(
            f"CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION workflow_reject_change()"
        )
        op.execute(f"ALTER TABLE {table} ENABLE ALWAYS TRIGGER {table}_immutable")
    for table in ("approvals", "notifications", "model_calls"):
        op.execute(f"DROP TRIGGER IF EXISTS {table}_erasure_guard ON {table}")
        op.execute(f"DROP FUNCTION IF EXISTS {table}_erasure_guard()")
    op.execute(f"REVOKE UPDATE (reason, reason_erased_at) ON appeals FROM {app_role}")
    op.execute(f"REVOKE UPDATE (context, payload, erased_at) ON approvals FROM {app_role}")
    op.execute(
        "REVOKE UPDATE (content, recipient_address, recipient_bidx, erased_at) "
        f"ON notifications FROM {app_role}"
    )
    op.execute(f"REVOKE UPDATE (request, response, erased_at) ON model_calls FROM {app_role}")
    op.execute(
        f"REVOKE UPDATE (sensitive, sensitive_erased_at) ON decision_records FROM {app_role}"
    )
    op.drop_column("appeals", "reason_erased_at")
    op.drop_column("approvals", "erased_at")
    op.drop_column("notifications", "erased_at")
    op.drop_column("model_calls", "erased_at")
    op.drop_column("decision_records", "sensitive_erased_at")
    op.drop_column("documents", "objects_erased_at")
    op.drop_column("documents", "erased_at")
    op.drop_constraint("ck_documents_status_valid", "documents", type_="check")
    op.create_check_constraint(
        "ck_documents_status_valid", "documents", f"status IN ({_OLD_STATUSES})"
    )
