# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Pairly Pay: invoices, how inbound emails were classified, corrections per vendor.

Tables (see ``pairly_pay.models``), all tenant tables with RLS forced and the standard
tenant policy. ``pay_invoices`` and ``pay_intake_emails``: SELECT, INSERT and UPDATE for
the application role (never DELETE). ``pay_invoice_corrections``: SELECT and INSERT only
(a correction that was made stays made).

``pay_invoices`` is unique per ``(org_id, document_id)``: the engine dedupes files by
hash per workflow, so the same file sent twice (by email, upload or API) is the same
document and stays one invoice.

``pay_pending_invoice_org_ids()``: SECURITY DEFINER, returns the ids of organisations
with an invoice received in the last 30 days that has no run yet, or one still
``extracting`` after 15 minutes (its run may have failed), and nothing else (the
pattern of migration 0006). The worker's ``pay_invoice_intake_start`` job uses it to
know which organisations to visit, then works in each under normal RLS. It switches on,
for its own statement only, the owner-only read policy ``app.pay_intake_scan``.

Revision ID: pay_0002
Revises: pay_0001
Create Date: 2026-10-14
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from pairly_core.migration_helpers import app_db_role, org_rls_statements

revision: str = "pay_0002"
down_revision: str | None = "pay_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

INVOICES = "pay_invoices"
EMAILS = "pay_intake_emails"
CORRECTIONS = "pay_invoice_corrections"


def _base(table: str) -> list[Any]:
    return [
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "org_id",
            sa.Uuid(),
            sa.ForeignKey(
                "organisations.id", ondelete="RESTRICT", name=f"fk_{table}_org_id_organisations"
            ),
            nullable=False,
        ),
    ]


def _ts(name: str, *, nullable: bool = True) -> sa.Column[Any]:
    if nullable:
        return sa.Column(name, sa.DateTime(timezone=True), nullable=True)
    return sa.Column(name, sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now())


def _jsonb_list(name: str) -> sa.Column[Any]:
    return sa.Column(
        name, postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")
    )


def _fk(table: str, name: str, target: str, *, nullable: bool = True) -> sa.Column[Any]:
    return sa.Column(
        name,
        sa.Uuid(),
        sa.ForeignKey(f"{target}.id", ondelete="RESTRICT", name=f"fk_{table}_{name}_{target}"),
        nullable=nullable,
    )


def _org_fk(table: str, column: str, parent: str, name: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["org_id", column],
        [f"{parent}.org_id", f"{parent}.id"],
        ondelete="RESTRICT",
        name=f"fk_{table}_{name}_org",
    )


def _emails() -> None:
    t = EMAILS
    op.create_table(
        t,
        *_base(t),
        _fk(t, "inbound_email_id", "inbound_emails", nullable=False),
        sa.Column("classification", sa.String(16), nullable=False),
        sa.Column("confidence", sa.String(8), nullable=False),
        _jsonb_list("reasons"),
        sa.Column("reference", sa.String(32), nullable=False),
        sa.Column("sender_domain", sa.String(253), nullable=True),
        sa.Column("forwarded_domain", sa.String(253), nullable=True),
        sa.Column("invoice_count", sa.Integer(), nullable=False, server_default="0"),
        _jsonb_list("supporting_document_ids"),
        sa.Column("receipt_sent", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("receipt_reason", sa.String(64), nullable=True),
        _ts("created_at", nullable=False),
        sa.UniqueConstraint("org_id", "id", name="uq_pay_intake_emails_org_id_id"),
        sa.UniqueConstraint(
            "org_id", "inbound_email_id", name="uq_pay_intake_emails_org_id_inbound_email_id"
        ),
        sa.CheckConstraint(
            "classification IN ('invoice', 'marketing', 'unsure')",
            name=f"ck_{t}_classification_valid",
        ),
    )
    op.create_index(f"ix_{t}_org_id", t, ["org_id"])
    op.create_index("ix_pay_intake_emails_created", t, ["org_id", "created_at"])


def _invoices() -> None:
    t = INVOICES
    op.create_table(
        t,
        *_base(t),
        sa.Column("reference", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("source", sa.String(16), nullable=False),
        _fk(t, "document_id", "documents", nullable=False),
        sa.Column("intake_email_id", sa.Uuid(), nullable=True),
        sa.Column("intake_reference", sa.String(32), nullable=True),
        _jsonb_list("sender_domains"),
        sa.Column("submitted_by_type", sa.String(16), nullable=False),
        sa.Column("submitted_by_id", sa.String(128), nullable=False),
        sa.Column(
            "submitted_by",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="RESTRICT", name=f"fk_{t}_submitted_by_users"),
            nullable=True,
        ),
        _fk(t, "run_id", "workflow_runs"),
        _fk(t, "extraction_id", "extractions"),
        sa.Column("extraction_source", sa.String(16), nullable=True),
        sa.Column("guardrail_report", postgresql.JSONB(), nullable=True),
        sa.Column("sanitized_sha256", sa.String(64), nullable=True),
        _jsonb_list("removed_spans"),
        sa.Column("vendor_id", sa.Uuid(), nullable=True),
        sa.Column(
            "vendor_match",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("vendor_name", sa.String(200), nullable=True),
        sa.Column("vendor_trn", sa.String(15), nullable=True),
        sa.Column("invoice_number", sa.String(64), nullable=True),
        sa.Column("invoice_date", sa.Date(), nullable=True),
        sa.Column("due_date", sa.Date(), nullable=True),
        sa.Column("currency", sa.String(3), nullable=True),
        sa.Column("po_number", sa.String(64), nullable=True),
        sa.Column("subtotal", sa.Numeric(20, 4), nullable=True),
        sa.Column("vat_amount", sa.Numeric(20, 4), nullable=True),
        sa.Column("total", sa.Numeric(20, 4), nullable=True),
        _jsonb_list("review_reasons"),
        _jsonb_list("decision_ids"),
        sa.Column(
            "reviewed_by",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="RESTRICT", name=f"fk_{t}_reviewed_by_users"),
            nullable=True,
        ),
        _ts("reviewed_at"),
        sa.Column(
            "rejected_by",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="RESTRICT", name=f"fk_{t}_rejected_by_users"),
            nullable=True,
        ),
        _ts("rejected_at"),
        sa.Column("reject_reason", sa.Text(), nullable=True),
        _ts("created_at", nullable=False),
        _ts("updated_at", nullable=False),
        sa.UniqueConstraint("org_id", "id", name="uq_pay_invoices_org_id_id"),
        sa.UniqueConstraint("org_id", "reference", name="uq_pay_invoices_org_id_reference"),
        sa.UniqueConstraint("org_id", "document_id", name="uq_pay_invoices_org_id_document_id"),
        _org_fk(t, "vendor_id", "pay_vendors", "vendor"),
        _org_fk(t, "intake_email_id", EMAILS, "intake_email"),
        sa.CheckConstraint(
            "status IN ('received', 'extracting', 'needs_review', 'ready', 'approved', "
            "'scheduled', 'paid', 'rejected')",
            name=f"ck_{t}_status_valid",
        ),
        sa.CheckConstraint("source IN ('email', 'upload', 'api')", name=f"ck_{t}_source_valid"),
        sa.CheckConstraint(
            "extraction_source IS NULL OR extraction_source IN ('model', 'einvoice')",
            name=f"ck_{t}_extraction_source_valid",
        ),
        sa.CheckConstraint(
            "currency IS NULL OR currency ~ '^[A-Z]{3}$'", name=f"ck_{t}_currency_format"
        ),
    )
    op.create_index(f"ix_{t}_org_id", t, ["org_id"])
    op.create_index("ix_pay_invoices_status", t, ["org_id", "status", "created_at"])
    op.create_index("ix_pay_invoices_vendor", t, ["org_id", "vendor_id", "created_at"])


def _corrections() -> None:
    t = CORRECTIONS
    op.create_table(
        t,
        *_base(t),
        sa.Column("invoice_id", sa.Uuid(), nullable=False),
        sa.Column("vendor_id", sa.Uuid(), nullable=True),
        _fk(t, "extraction_id", "extractions", nullable=False),
        _fk(t, "correction_id", "extraction_corrections", nullable=False),
        sa.Column("field_path", sa.String(200), nullable=False),
        sa.Column("field_pattern", sa.String(200), nullable=False),
        sa.Column("old_value", postgresql.JSONB(), nullable=True),
        sa.Column("new_value", postgresql.JSONB(), nullable=True),
        sa.Column(
            "corrected_by",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="RESTRICT", name=f"fk_{t}_corrected_by_users"),
            nullable=False,
        ),
        _ts("created_at", nullable=False),
        _org_fk(t, "invoice_id", INVOICES, "invoice"),
        _org_fk(t, "vendor_id", "pay_vendors", "vendor"),
    )
    op.create_index(f"ix_{t}_org_id", t, ["org_id"])
    op.create_index("ix_pay_invoice_corrections_vendor", t, ["org_id", "vendor_id", "created_at"])


def _pending_finder(app_role: str) -> None:
    op.execute(
        f"CREATE POLICY {INVOICES}_intake_scan ON {INVOICES} FOR SELECT TO CURRENT_USER "
        "USING (current_setting('app.pay_intake_scan', true) = 'on')"
    )
    op.execute(
        """
        CREATE FUNCTION pay_pending_invoice_org_ids() RETURNS SETOF uuid
        LANGUAGE plpgsql VOLATILE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        DECLARE
            previous text := current_setting('app.pay_intake_scan', true);
            ids uuid[];
        BEGIN
            PERFORM set_config('app.pay_intake_scan', 'on', true);
            SELECT array_agg(DISTINCT i.org_id ORDER BY i.org_id) INTO ids
              FROM public.pay_invoices i
             WHERE (i.status = 'received'
                    AND i.run_id IS NULL
                    AND i.created_at > now() - interval '30 days')
                OR (i.status = 'extracting'
                    AND i.updated_at < now() - interval '15 minutes');
            PERFORM set_config('app.pay_intake_scan', coalesce(previous, ''), true);
            RETURN QUERY SELECT unnest(coalesce(ids, '{}'::uuid[]));
        END
        $$
        """
    )
    op.execute("REVOKE ALL ON FUNCTION pay_pending_invoice_org_ids() FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION pay_pending_invoice_org_ids() TO {app_role}")


def upgrade() -> None:
    app_role = app_db_role()
    _emails()
    _invoices()
    _corrections()
    for table in (EMAILS, INVOICES):
        for stmt in org_rls_statements(table, app_role, privileges="SELECT, INSERT, UPDATE"):
            op.execute(stmt)
    for stmt in org_rls_statements(CORRECTIONS, app_role, privileges="SELECT, INSERT"):
        op.execute(stmt)
    _pending_finder(app_role)


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS pay_pending_invoice_org_ids()")
    for table in (CORRECTIONS, INVOICES, EMAILS):
        op.drop_table(table)
