# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Pairly Pay: bank statements and their reconciliation with payments.

* ``pay_bank_statements``: an imported statement (CSV or camt.053), its file digest
  (unique per organisation: the same file is refused twice), dates and counts.
* ``pay_bank_statement_lines``: its booked lines (signed amounts, a payment out is
  negative), the counterparty IBAN encrypted with a blind index, and what each line
  was matched with: a paid payment run item (and its invoice) or a whole run paid as
  one batch. A partial unique index lets one line at most explain an item or a run.

Tenant tables with RLS forced and the standard tenant policy; the application role
gets SELECT, INSERT and UPDATE (a line's match), never DELETE. Both are closed to
portal sessions (``portal_guard_statements``).

``pay_reconciliation_org_ids()``: SECURITY DEFINER, the ids of organisations with an
unmatched statement line and nothing else (the pattern of ``pay_0001``). The worker's
``pay_reconciliation`` job uses it to know which organisations to visit, then works in
each under normal RLS as the application role.

Revision ID: pay_0016
Revises: pay_0015
Create Date: 2026-10-15
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from pairly_core.migration_helpers import (
    app_db_role,
    drop_portal_guard_statements,
    org_rls_statements,
    portal_guard_statements,
)

revision: str = "pay_0016"
down_revision: str | None = "pay_0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = "0023"

S = "pay_bank_statements"
L = "pay_bank_statement_lines"


def _org(table: str) -> sa.Column[Any]:
    return sa.Column(
        "org_id",
        sa.Uuid(),
        sa.ForeignKey(
            "organisations.id", ondelete="RESTRICT", name=f"fk_{table}_org_id_organisations"
        ),
        nullable=False,
    )


def _now(name: str) -> sa.Column[Any]:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now())


def _id() -> sa.Column[Any]:
    return sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()"))


def _statements() -> None:
    op.create_table(
        S,
        _id(),
        _org(S),
        sa.Column("reference", sa.String(16), nullable=False),
        sa.Column("format", sa.String(8), nullable=False),
        sa.Column("file_sha256", sa.String(64), nullable=False),
        sa.Column("bank_statement_id", sa.String(64), nullable=True),
        sa.Column("account_masked", sa.String(40), nullable=True),
        sa.Column("from_date", sa.Date(), nullable=False),
        sa.Column("to_date", sa.Date(), nullable=False),
        sa.Column("line_count", sa.Integer(), nullable=False),
        sa.Column(
            "imported_by",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="RESTRICT", name=f"fk_{S}_imported_by_users"),
            nullable=False,
        ),
        _now("created_at"),
        sa.UniqueConstraint("org_id", "id", name=f"uq_{S}_org_id_id"),
        sa.UniqueConstraint("org_id", "reference", name=f"uq_{S}_org_id_reference"),
        sa.UniqueConstraint("org_id", "file_sha256", name=f"uq_{S}_org_id_file_sha256"),
        sa.CheckConstraint("format IN ('csv', 'camt053')", name=f"ck_{S}_format_valid"),
        sa.CheckConstraint("file_sha256 ~ '^[0-9a-f]{64}$'", name=f"ck_{S}_file_sha256_format"),
        sa.CheckConstraint("line_count > 0", name=f"ck_{S}_line_count_positive"),
    )
    op.create_index(f"ix_{S}_org_id", S, ["org_id"])
    op.create_index(f"ix_{S}_created", S, ["org_id", "created_at"])


def _lines() -> None:
    op.create_table(
        L,
        _id(),
        _org(L),
        sa.Column("statement_id", sa.Uuid(), nullable=False),
        sa.Column("line_no", sa.Integer(), nullable=False),
        sa.Column("booking_date", sa.Date(), nullable=False),
        sa.Column("amount", sa.Numeric(20, 4), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("reference", sa.String(140), nullable=True),
        sa.Column("counterparty_iban", sa.Text(), nullable=True),
        sa.Column("counterparty_iban_bidx", sa.String(64), nullable=True),
        sa.Column("counterparty_iban_masked", sa.String(40), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("match_kind", sa.String(8), nullable=True),
        sa.Column("payment_run_id", sa.Uuid(), nullable=True),
        sa.Column("payment_run_item_id", sa.Uuid(), nullable=True),
        sa.Column("invoice_id", sa.Uuid(), nullable=True),
        sa.Column(
            "match_reasons",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("unmatched_reason", sa.String(32), nullable=True),
        sa.Column("matched_at", sa.DateTime(timezone=True), nullable=True),
        _now("created_at"),
        _now("updated_at"),
        sa.UniqueConstraint("org_id", "id", name=f"uq_{L}_org_id_id"),
        sa.UniqueConstraint("org_id", "statement_id", "line_no", name=f"uq_{L}_line"),
        sa.ForeignKeyConstraint(
            ["org_id", "statement_id"],
            [f"{S}.org_id", f"{S}.id"],
            ondelete="RESTRICT",
            name=f"fk_{L}_statement_org",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "payment_run_item_id"],
            ["pay_payment_run_items.org_id", "pay_payment_run_items.id"],
            ondelete="RESTRICT",
            name=f"fk_{L}_item_org",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "payment_run_id"],
            ["pay_payment_runs.org_id", "pay_payment_runs.id"],
            ondelete="RESTRICT",
            name=f"fk_{L}_run_org",
        ),
        sa.CheckConstraint(
            "status IN ('unmatched', 'matched', 'ignored')", name=f"ck_{L}_status_valid"
        ),
        sa.CheckConstraint(
            "match_kind IS NULL OR match_kind IN ('item', 'run')", name=f"ck_{L}_match_kind_valid"
        ),
        sa.CheckConstraint("currency ~ '^[A-Z]{3}$'", name=f"ck_{L}_currency_format"),
        sa.CheckConstraint("amount <> 0", name=f"ck_{L}_amount_not_zero"),
        sa.CheckConstraint(
            "counterparty_iban_bidx IS NULL OR counterparty_iban_bidx ~ '^[0-9a-f]{64}$'",
            name=f"ck_{L}_iban_bidx_format",
        ),
        sa.CheckConstraint(
            "status <> 'matched' OR (match_kind = 'item' AND payment_run_item_id IS NOT NULL "
            "AND invoice_id IS NOT NULL AND payment_run_id IS NOT NULL) "
            "OR (match_kind = 'run' AND payment_run_id IS NOT NULL "
            "AND payment_run_item_id IS NULL)",
            name=f"ck_{L}_matched_has_target",
        ),
        sa.CheckConstraint(
            "status = 'matched' OR (match_kind IS NULL AND payment_run_id IS NULL "
            "AND payment_run_item_id IS NULL AND invoice_id IS NULL)",
            name=f"ck_{L}_unmatched_has_no_target",
        ),
    )
    op.create_index(f"ix_{L}_org_id", L, ["org_id"])
    op.create_index(f"ix_{L}_status", L, ["org_id", "status", "booking_date"])
    op.create_index(
        f"uq_{L}_item",
        L,
        ["org_id", "payment_run_item_id"],
        unique=True,
        postgresql_where=sa.text("payment_run_item_id IS NOT NULL"),
    )
    op.create_index(
        f"uq_{L}_run",
        L,
        ["org_id", "payment_run_id"],
        unique=True,
        postgresql_where=sa.text("match_kind = 'run'"),
    )


def _org_finder(app_role: str) -> None:
    op.execute(
        f"CREATE POLICY {L}_reconcile_scan ON {L} FOR SELECT TO CURRENT_USER "
        "USING (current_setting('app.pay_reconcile_scan', true) = 'on')"
    )
    op.execute(
        """
        CREATE FUNCTION pay_reconciliation_org_ids() RETURNS SETOF uuid
        LANGUAGE plpgsql VOLATILE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        DECLARE
            previous text := current_setting('app.pay_reconcile_scan', true);
            ids uuid[];
        BEGIN
            PERFORM set_config('app.pay_reconcile_scan', 'on', true);
            SELECT array_agg(DISTINCT l.org_id ORDER BY l.org_id) INTO ids
              FROM public.pay_bank_statement_lines l
             WHERE l.status = 'unmatched';
            PERFORM set_config('app.pay_reconcile_scan', coalesce(previous, ''), true);
            RETURN QUERY SELECT unnest(coalesce(ids, '{}'::uuid[]));
        END
        $$
        """
    )
    op.execute("REVOKE ALL ON FUNCTION pay_reconciliation_org_ids() FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION pay_reconciliation_org_ids() TO {app_role}")


def upgrade() -> None:
    app_role = app_db_role()
    _statements()
    _lines()
    for table in (S, L):
        for stmt in org_rls_statements(table, app_role, privileges="SELECT, INSERT, UPDATE"):
            op.execute(stmt)
        for stmt in portal_guard_statements(table):
            op.execute(stmt)
    _org_finder(app_role)


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS pay_reconciliation_org_ids()")
    for table in (L, S):
        for stmt in drop_portal_guard_statements(table):
            op.execute(stmt)
        op.drop_table(table)
