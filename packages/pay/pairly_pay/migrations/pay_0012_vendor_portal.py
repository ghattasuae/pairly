# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Pairly Pay: the vendor portal and credit notes (brief 4.7, 5.1 item 11).

New tables (tenant tables, RLS forced, standard tenant policy; SELECT, INSERT, UPDATE,
never DELETE; see ``pairly_pay.portal.models``): ``pay_portal_contacts``,
``pay_portal_tokens``, ``pay_portal_sessions``.

``pay_invoices``: source ``vendor_portal``; ``document_type`` (``invoice`` or
``credit_note``), the invoice a credit note is for, the credit applied to an invoice
(``credit_total``), when and by whom a credit note was applied (status ``applied``), and
the portal contact who sent it. A credit note is never approved, scheduled or paid.

``pay_payment_run_items.execution_date``: the run's date per item (filled for existing
items), so the portal shows an expected payment date without reading any run.

``pay_messages``: ``channel`` (``email`` or ``portal``), ``direction`` (``outbound`` from
AP, ``inbound`` from a vendor's contact), the invoice's vendor (filled for existing rows)
and the portal contact. ``submitted_by`` is empty for a vendor's message. Statuses
``posted`` (an AP reply shown in the portal) and ``received`` (a vendor's message);
recipient kind ``ap``.

The portal subject (``pairly_core.db.set_portal_subject``, setting
``app.portal_subject``, ``pay_vendor:<uuid>`` for a vendor portal session) adds a second,
restrictive layer to Row Level Security, like Hire's candidate portal (``hire_0004b``).
``pay_portal_active()`` says whether the transaction acts for any portal person (a
candidate too); ``pay_portal_vendor()`` is the vendor's id (NULL for any other portal
person, so nothing matches). While a portal subject is set:

* ``pay_vendors`` shows and accepts only the vendor's own row;
* ``pay_vendor_bank_accounts``, ``pay_bank_change_tickets``, ``pay_invoices`` and the
  three portal tables only rows of that vendor (``vendor_id``);
* ``pay_messages`` only that vendor's thread: its own messages and what AP sent to the
  vendor (queued or posted, never a draft, never a message to a requester); a portal
  session may only add inbound messages;
* ``pay_payment_run_items`` only that vendor's items, read only;
* ``pay_settings`` read only (the cooling-off period a bank change needs);
* every other Pay table (calls, invites, onboarding sessions, intake emails, corrections,
  payment runs, purchase orders, goods receipts, coding) shows nothing at all.

Staff, agents and jobs never set a portal subject, so these policies change nothing for
them.

System templates ``pay.vendor_portal.login_link`` and ``pay.vendor_portal.reply`` (email,
en and ar, ``pairly_pay.portal.templates``) as version 1 shared rows.

Revision ID: pay_0012
Revises: pay_0011
Create Date: 2026-10-14
"""

import json
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op

from pairly_core.migration_helpers import app_db_role, org_rls_statements

revision: str = "pay_0012"
down_revision: str | None = "pay_0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = "0021"

_RW = "SELECT, INSERT, UPDATE"
_SYSTEM_ACTOR = "pairly.migrations.pay_0012"
INV = "pay_invoices"
MSG = "pay_messages"
ITEMS = "pay_payment_run_items"
_NEW = ("pay_portal_contacts", "pay_portal_tokens", "pay_portal_sessions")

_INVOICE_STATUSES_OLD = (
    "'received', 'extracting', 'needs_review', 'ready', 'awaiting_approval', 'approved', "
    "'scheduled', 'paid', 'rejected'"
)
_INVOICE_STATUSES_NEW = _INVOICE_STATUSES_OLD + ", 'applied'"
_SOURCES_OLD = "'email', 'upload', 'api'"
_SOURCES_NEW = _SOURCES_OLD + ", 'vendor_portal'"
_MESSAGE_STATUSES_OLD = (
    "'submitted', 'awaiting_approval', 'queued', 'declined', 'changes_requested', 'failed'"
)
_MESSAGE_STATUSES_NEW = _MESSAGE_STATUSES_OLD + ", 'posted', 'received'"

_BY_VENDOR_ID = (
    "pay_vendor_bank_accounts",
    "pay_bank_change_tickets",
    "pay_invoices",
    *_NEW,
)
_HIDDEN = (
    "pay_bank_change_calls",
    "pay_vendor_invites",
    "pay_vendor_onboarding_sessions",
    "pay_intake_emails",
    "pay_invoice_corrections",
    "pay_payment_runs",
    "pay_purchase_orders",
    "pay_purchase_order_lines",
    "pay_goods_receipts",
    "pay_goods_receipt_lines",
    "pay_coding_lists",
    "pay_coding_entries",
    "pay_invoice_line_codings",
    "pay_coding_corrections",
)


# The naming convention prefixes every check constraint name with ``ck_<table>_``; the
# older Pay revisions passed full names, so theirs carry the prefix twice. Both are
# named here exactly as the convention makes them.
def _check(table: str, name: str, rule: str) -> None:
    op.create_check_constraint(name, table, rule)


def _replace_check(table: str, name: str, rule: str) -> None:
    op.drop_constraint(f"ck_{table}_{name}", table, type_="check")
    op.create_check_constraint(f"ck_{table}_{name}", table, rule)


def _uuid_pk() -> sa.Column[Any]:
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


def _ts(name: str, *, nullable: bool = False) -> sa.Column[Any]:
    if nullable:
        return sa.Column(name, sa.DateTime(timezone=True), nullable=True)
    return sa.Column(name, sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now())


def _user(table: str, name: str, *, nullable: bool = True) -> sa.Column[Any]:
    return sa.Column(
        name,
        sa.Uuid(),
        sa.ForeignKey("users.id", ondelete="RESTRICT", name=f"fk_{table}_{name}_users"),
        nullable=nullable,
    )


def _vendor_fk(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["org_id", "vendor_id"],
        ["pay_vendors.org_id", "pay_vendors.id"],
        ondelete="RESTRICT",
        name=f"fk_{table}_vendor_org",
    )


def _contact_fk(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["org_id", "contact_id"],
        ["pay_portal_contacts.org_id", "pay_portal_contacts.id"],
        ondelete="RESTRICT",
        name=f"fk_{table}_contact_org",
    )


def _finish(table: str) -> None:
    op.create_index(f"ix_{table}_org_id", table, ["org_id"])
    for stmt in org_rls_statements(table, app_db_role(), privileges=_RW):
        op.execute(stmt)


def upgrade() -> None:
    _invoices()
    _run_items()
    _messages()
    _portal_tables()
    _portal_policies()
    _seed_templates()


def _invoices() -> None:
    _replace_check(INV, "status_valid", f"status IN ({_INVOICE_STATUSES_NEW})")
    _replace_check(INV, "source_valid", f"source IN ({_SOURCES_NEW})")
    op.add_column(
        INV, sa.Column("document_type", sa.String(16), nullable=False, server_default="invoice")
    )
    op.add_column(INV, sa.Column("credit_for_invoice_id", sa.Uuid(), nullable=True))
    op.add_column(
        INV, sa.Column("credit_total", sa.Numeric(20, 4), nullable=False, server_default="0")
    )
    op.add_column(INV, _ts("credit_applied_at", nullable=True))
    op.add_column(INV, _user(INV, "credit_applied_by"))
    op.add_column(INV, sa.Column("portal_contact_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_pay_invoices_credit_for_org",
        INV,
        INV,
        ["org_id", "credit_for_invoice_id"],
        ["org_id", "id"],
        ondelete="RESTRICT",
    )
    _check(INV, "document_type_valid", "document_type IN ('invoice', 'credit_note')")
    _check(
        INV,
        "credit_link_only_on_credit_notes",
        "document_type = 'credit_note' OR credit_for_invoice_id IS NULL",
    )
    _check(
        INV,
        "applied_is_credit_note",
        "status <> 'applied' OR (document_type = 'credit_note' "
        "AND credit_for_invoice_id IS NOT NULL AND credit_applied_at IS NOT NULL)",
    )
    _check(
        INV,
        "credit_note_never_paid",
        "document_type = 'invoice' OR status NOT IN "
        "('awaiting_approval', 'approved', 'scheduled', 'paid')",
    )
    _check(INV, "credit_total_not_negative", "credit_total >= 0")
    op.create_index("ix_pay_invoices_credit_for", INV, ["org_id", "credit_for_invoice_id"])


def _run_items() -> None:
    op.add_column(ITEMS, sa.Column("execution_date", sa.Date(), nullable=True))
    op.execute(f"ALTER TABLE {ITEMS} NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE pay_payment_runs NO FORCE ROW LEVEL SECURITY")
    op.execute(
        "UPDATE pay_payment_run_items i SET execution_date = r.execution_date "
        "FROM pay_payment_runs r WHERE r.id = i.payment_run_id AND r.org_id = i.org_id "
        "AND i.execution_date IS NULL"
    )
    op.execute("ALTER TABLE pay_payment_runs FORCE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {ITEMS} FORCE ROW LEVEL SECURITY")


def _messages() -> None:
    op.add_column(MSG, sa.Column("channel", sa.String(8), nullable=False, server_default="email"))
    op.add_column(
        MSG, sa.Column("direction", sa.String(8), nullable=False, server_default="outbound")
    )
    op.add_column(MSG, sa.Column("vendor_id", sa.Uuid(), nullable=True))
    op.add_column(MSG, sa.Column("portal_contact_id", sa.Uuid(), nullable=True))
    op.alter_column(MSG, "submitted_by", existing_type=sa.Uuid(), nullable=True)
    op.execute(f"ALTER TABLE {MSG} NO FORCE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {INV} NO FORCE ROW LEVEL SECURITY")
    op.execute(
        "UPDATE pay_messages m SET vendor_id = i.vendor_id FROM pay_invoices i "
        "WHERE i.id = m.invoice_id AND i.org_id = m.org_id AND m.vendor_id IS NULL"
    )
    op.execute(f"ALTER TABLE {INV} FORCE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {MSG} FORCE ROW LEVEL SECURITY")
    _replace_check(MSG, "status_valid", f"status IN ({_MESSAGE_STATUSES_NEW})")
    _replace_check(MSG, "recipient_valid", "recipient_kind IN ('vendor', 'requester', 'ap')")
    op.create_foreign_key(
        "fk_pay_messages_vendor_org",
        MSG,
        "pay_vendors",
        ["org_id", "vendor_id"],
        ["org_id", "id"],
        ondelete="RESTRICT",
    )
    _check(MSG, "channel_valid", "channel IN ('email', 'portal')")
    _check(MSG, "direction_valid", "direction IN ('outbound', 'inbound')")
    _check(MSG, "outbound_has_author", "direction = 'inbound' OR submitted_by IS NOT NULL")
    _check(
        MSG,
        "inbound_from_portal",
        "direction = 'outbound' OR (portal_contact_id IS NOT NULL AND channel = 'portal' "
        "AND recipient_kind = 'ap' AND status = 'received')",
    )
    _check(MSG, "portal_statuses", "status NOT IN ('posted', 'received') OR channel = 'portal'")
    op.create_index("ix_pay_messages_vendor", MSG, ["org_id", "vendor_id", "created_at"])


def _portal_tables() -> None:
    t = "pay_portal_contacts"
    op.create_table(
        t,
        _uuid_pk(),
        _org(t),
        sa.Column("vendor_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("email_bidx", sa.String(64), nullable=False),
        sa.Column("locale", sa.String(2), nullable=False, server_default="en"),
        sa.Column("status", sa.String(16), nullable=False),
        _user(t, "created_by", nullable=False),
        _ts("created_at"),
        _ts("revoked_at", nullable=True),
        _user(t, "revoked_by"),
        _ts("last_sign_in_at", nullable=True),
        sa.UniqueConstraint("org_id", "id", name="uq_pay_portal_contacts_org_id_id"),
        _vendor_fk(t),
        sa.CheckConstraint("status IN ('active', 'revoked')", name=f"ck_{t}_status_valid"),
        sa.CheckConstraint("email_bidx ~ '^[0-9a-f]{64}$'", name=f"ck_{t}_email_bidx_format"),
        sa.CheckConstraint("locale IN ('en', 'ar')", name=f"ck_{t}_locale_valid"),
        sa.CheckConstraint(
            "(status = 'revoked') = (revoked_at IS NOT NULL)", name=f"ck_{t}_revoked_when"
        ),
    )
    op.create_index("ix_pay_portal_contacts_vendor", t, ["org_id", "vendor_id", "created_at"])
    op.create_index(
        "uq_pay_portal_contacts_active_email",
        t,
        ["org_id", "email_bidx"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )
    _finish(t)

    for t, extra in (
        (
            "pay_portal_tokens",
            [
                sa.Column("purpose", sa.String(16), nullable=False),
                sa.Column("locale", sa.String(2), nullable=False),
                _ts("created_at"),
                sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
                _ts("used_at", nullable=True),
                sa.CheckConstraint(
                    "expires_at > created_at", name="ck_pay_portal_tokens_expiry_after_creation"
                ),
                sa.CheckConstraint(
                    "purpose IN ('request', 'invite')", name="ck_pay_portal_tokens_purpose_valid"
                ),
            ],
        ),
        (
            "pay_portal_sessions",
            [
                sa.Column("token_id", sa.Uuid(), nullable=True),
                _ts("created_at"),
                sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
                _ts("last_seen_at", nullable=True),
                _ts("revoked_at", nullable=True),
            ],
        ),
    ):
        op.create_table(
            t,
            _uuid_pk(),
            _org(t),
            sa.Column("contact_id", sa.Uuid(), nullable=False),
            sa.Column("vendor_id", sa.Uuid(), nullable=False),
            sa.Column("secret_hash", sa.String(64), nullable=False),
            *extra,
            _vendor_fk(t),
            _contact_fk(t),
            sa.CheckConstraint("secret_hash ~ '^[0-9a-f]{64}$'", name=f"ck_{t}_secret_hash_format"),
        )
        op.create_index(f"ix_{t}_contact", t, ["org_id", "contact_id", "created_at"])
        _finish(t)


def _portal_policies() -> None:
    op.execute(
        """
        CREATE FUNCTION pay_portal_active() RETURNS boolean
        LANGUAGE sql STABLE
        AS $$ SELECT coalesce(current_setting('app.portal_subject', true), '') <> '' $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION pay_portal_vendor() RETURNS uuid
        LANGUAGE sql STABLE
        AS $$
            SELECT CASE
                WHEN current_setting('app.portal_subject', true)
                     ~ '^pay_vendor:[0-9a-f-]{36}$'
                THEN substr(current_setting('app.portal_subject', true), 12)::uuid
            END
        $$
        """
    )

    def policy(table: str, using: str, check: str) -> None:
        op.execute(
            f"CREATE POLICY {table}_portal_subject ON {table} AS RESTRICTIVE "
            f"USING ({using}) WITH CHECK ({check})"
        )

    own = "NOT pay_portal_active() OR {column} = pay_portal_vendor()"
    policy("pay_vendors", own.format(column="id"), own.format(column="id"))
    for table in _BY_VENDOR_ID:
        policy(table, own.format(column="vendor_id"), own.format(column="vendor_id"))
    policy(
        MSG,
        "NOT pay_portal_active() OR (vendor_id = pay_portal_vendor() AND ("
        "direction = 'inbound' OR (recipient_kind = 'vendor' "
        "AND status IN ('queued', 'posted'))))",
        "NOT pay_portal_active() OR (vendor_id = pay_portal_vendor() AND direction = 'inbound')",
    )
    policy(ITEMS, own.format(column="vendor_id"), "NOT pay_portal_active()")
    policy("pay_settings", "true", "NOT pay_portal_active()")
    for table in _HIDDEN:
        policy(table, "NOT pay_portal_active()", "NOT pay_portal_active()")


def _seed_templates() -> None:
    from pairly_core.notifications.templating import (
        TemplateSource,
        declarations_json,
        lint_source,
        parse_declarations,
    )
    from pairly_pay.portal.templates import PORTAL_TEMPLATES

    bind = op.get_bind()
    op.execute("ALTER TABLE templates NO FORCE ROW LEVEL SECURITY")
    insert = sa.text(
        "INSERT INTO templates (org_id, key, channel, locale, version, category, subject, "
        "body, body_html, variables, requires_approval, created_by_type, created_by_id) "
        "SELECT NULL, CAST(:key AS text), CAST(:channel AS text), CAST(:locale AS text), 1, "
        "CAST(:category AS text), CAST(:subject AS text), CAST(:body AS text), "
        "CAST(:body_html AS text), CAST(:variables AS jsonb), "
        "CAST(:requires_approval AS boolean), 'system', CAST(:actor AS text) "
        "WHERE NOT EXISTS (SELECT 1 FROM templates WHERE org_id IS NULL "
        "AND key = CAST(:key AS text) AND channel = CAST(:channel AS text) "
        "AND locale = CAST(:locale AS text) AND version = 1)"
    )
    for t in PORTAL_TEMPLATES:
        specs = parse_declarations(t.variables)
        source = TemplateSource(t.channel, t.locale, t.subject, t.body, t.body_html, specs)  # type: ignore[arg-type]
        if any(i.severity == "error" for i in lint_source(source)):
            raise RuntimeError(f"system template {t.key} ({t.channel}, {t.locale}) is invalid")
        bind.execute(
            insert,
            {
                "key": t.key,
                "channel": t.channel,
                "locale": t.locale,
                "category": t.category,
                "subject": t.subject,
                "body": t.body,
                "body_html": t.body_html,
                "variables": json.dumps(declarations_json(specs)),
                "requires_approval": t.requires_approval,
                "actor": _SYSTEM_ACTOR,
            },
        )
    op.execute("ALTER TABLE templates FORCE ROW LEVEL SECURITY")


def downgrade() -> None:
    for table in ("pay_vendors", MSG, ITEMS, "pay_settings", *_HIDDEN, *_BY_VENDOR_ID):
        if table not in _NEW:
            op.execute(f"DROP POLICY IF EXISTS {table}_portal_subject ON {table}")
    for table in reversed(_NEW):
        op.drop_table(table)
    op.execute("DROP FUNCTION IF EXISTS pay_portal_vendor()")
    op.execute("DROP FUNCTION IF EXISTS pay_portal_active()")

    op.drop_index("ix_pay_messages_vendor", table_name=MSG)
    for name in (
        "portal_statuses",
        "inbound_from_portal",
        "outbound_has_author",
        "direction_valid",
        "channel_valid",
    ):
        op.drop_constraint(name, MSG, type_="check")
    op.drop_constraint("fk_pay_messages_vendor_org", MSG, type_="foreignkey")
    _replace_check(MSG, "recipient_valid", "recipient_kind IN ('vendor', 'requester')")
    op.execute(f"ALTER TABLE {MSG} NO FORCE ROW LEVEL SECURITY")
    op.execute("UPDATE pay_messages SET status = 'failed' WHERE status IN ('posted', 'received')")
    op.execute("DELETE FROM pay_messages WHERE direction = 'inbound'")
    op.execute(f"ALTER TABLE {MSG} FORCE ROW LEVEL SECURITY")
    _replace_check(MSG, "status_valid", f"status IN ({_MESSAGE_STATUSES_OLD})")
    op.alter_column(MSG, "submitted_by", existing_type=sa.Uuid(), nullable=False)
    for column in ("portal_contact_id", "vendor_id", "direction", "channel"):
        op.drop_column(MSG, column)

    op.drop_column(ITEMS, "execution_date")

    op.drop_index("ix_pay_invoices_credit_for", table_name=INV)
    for name in (
        "credit_total_not_negative",
        "credit_note_never_paid",
        "applied_is_credit_note",
        "credit_link_only_on_credit_notes",
        "document_type_valid",
    ):
        op.drop_constraint(name, INV, type_="check")
    op.drop_constraint("fk_pay_invoices_credit_for_org", INV, type_="foreignkey")
    for column in (
        "portal_contact_id",
        "credit_applied_by",
        "credit_applied_at",
        "credit_total",
        "credit_for_invoice_id",
        "document_type",
    ):
        op.drop_column(INV, column)
    op.execute(f"ALTER TABLE {INV} NO FORCE ROW LEVEL SECURITY")
    op.execute("UPDATE pay_invoices SET status = 'rejected' WHERE status = 'applied'")
    op.execute("UPDATE pay_invoices SET source = 'upload' WHERE source = 'vendor_portal'")
    op.execute(f"ALTER TABLE {INV} FORCE ROW LEVEL SECURITY")
    _replace_check(INV, "source_valid", f"source IN ({_SOURCES_OLD})")
    _replace_check(INV, "status_valid", f"status IN ({_INVOICE_STATUSES_OLD})")

    op.execute("ALTER TABLE templates NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE notifications NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE templates DISABLE TRIGGER templates_immutable")
    op.execute(
        sa.text(
            "DELETE FROM templates t WHERE t.org_id IS NULL AND t.created_by_id = :actor "
            "AND NOT EXISTS (SELECT 1 FROM notifications n WHERE n.template_id = t.id)"
        ).bindparams(actor=_SYSTEM_ACTOR)
    )
    op.execute("ALTER TABLE templates ENABLE TRIGGER templates_immutable")
    op.execute("ALTER TABLE notifications FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE templates FORCE ROW LEVEL SECURITY")
