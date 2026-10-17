# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Pairly Pay: vendors, bank accounts, bank-detail change tickets, onboarding links.

The first revision of the ``pay`` branch (``PayPlugin.migrations_dir``). It starts from
no parent and ``depends_on`` core 0020, the core head when it was written.

Tables (see ``pairly_pay.models``), all tenant tables with RLS forced and the standard
tenant policy. The application role gets SELECT, INSERT and UPDATE, never DELETE; the
call records get SELECT and INSERT only (a call that happened cannot be edited).

``pay_bank_account_guard()``: a trigger on ``pay_vendor_bank_accounts`` that refuses
any change to the plain columns identifying an account (vendor, country, bank code,
last four digits, bank name, SWIFT, creator) and any status move other than
``proposed`` to ``active`` or ``rejected``, and ``active`` to ``retired``. New details are
a new row. The encrypted columns may change only through key rotation (re-wrapping
keeps the plaintext); the change ticket's keyed digest catches anything else.

``pay_due_bank_change_org_ids()``: SECURITY DEFINER, returns the ids of organisations
with a change ticket whose cooling-off period is over, and nothing else (the pattern of
migration 0006). The worker's ``pay_bank_change_activation`` job uses it to know which
organisations to visit, then works in each under normal RLS as the application role.
It switches on, for its own statement only, the owner-only read policy
``app.pay_due_scan`` on ``pay_bank_change_tickets``.

Revision ID: pay_0001
Revises: (none; branch ``pay``, depends on core 0020)
Create Date: 2026-10-14
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from pairly_core.migration_helpers import app_db_role, org_rls_statements

revision: str = "pay_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = ("pay",)
depends_on: str | Sequence[str] | None = "0020"

VENDORS = "pay_vendors"
ACCOUNTS = "pay_vendor_bank_accounts"
TICKETS = "pay_bank_change_tickets"
CALLS = "pay_bank_change_calls"
INVITES = "pay_vendor_invites"
SESSIONS = "pay_vendor_onboarding_sessions"
SETTINGS = "pay_settings"
_ALL = (SETTINGS, SESSIONS, INVITES, CALLS, TICKETS, ACCOUNTS, VENDORS)
_OPEN = "'awaiting_approval', 'awaiting_verification', 'cooling_off'"


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


def _user(table: str, name: str, *, nullable: bool = True) -> sa.Column[Any]:
    return sa.Column(
        name,
        sa.Uuid(),
        sa.ForeignKey("users.id", ondelete="RESTRICT", name=f"fk_{table}_{name}_users"),
        nullable=nullable,
    )


def _org_fk(table: str, column: str, parent: str, name: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["org_id", column],
        [f"{parent}.org_id", f"{parent}.id"],
        ondelete="RESTRICT",
        name=f"fk_{table}_{name}_org",
    )


def _vendors() -> None:
    t = VENDORS
    op.create_table(
        t,
        *_base(t),
        sa.Column("reference", sa.String(16), nullable=False),
        sa.Column("legal_name", sa.String(200), nullable=False),
        sa.Column("legal_name_ar", sa.String(200), nullable=True),
        sa.Column("trade_name", sa.String(200), nullable=True),
        sa.Column("name_key", sa.String(300), nullable=False),
        sa.Column("country", sa.String(2), nullable=False),
        sa.Column("trade_licence", sa.String(64), nullable=True),
        sa.Column("trn", sa.String(15), nullable=True),
        sa.Column("trn_status", sa.String(16), nullable=False, server_default="none"),
        sa.Column(
            "address", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column("contacts", sa.Text(), nullable=True),
        sa.Column("phone_on_file", sa.Text(), nullable=True),
        sa.Column("phone_bidx", sa.String(64), nullable=True),
        _ts("phone_changed_at"),
        _jsonb_list("email_domains"),
        sa.Column("category", sa.String(64), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("payment_hold", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("hold_reason", sa.String(32), nullable=True),
        _ts("hold_set_at"),
        _jsonb_list("review_reasons"),
        _jsonb_list("document_ids"),
        sa.Column("created_by_type", sa.String(16), nullable=False),
        sa.Column("created_by_id", sa.String(128), nullable=False),
        _ts("created_at", nullable=False),
        _ts("updated_at", nullable=False),
        _ts("activated_at"),
        sa.UniqueConstraint("org_id", "id", name="uq_pay_vendors_org_id_id"),
        sa.UniqueConstraint("org_id", "reference", name="uq_pay_vendors_org_id_reference"),
        sa.CheckConstraint(
            "status IN ('pending', 'active', 'rejected', 'archived')", name=f"ck_{t}_status_valid"
        ),
        sa.CheckConstraint("source IN ('staff', 'onboarding')", name=f"ck_{t}_source_valid"),
        sa.CheckConstraint(
            "hold_reason IS NULL OR hold_reason IN ('bank_change', 'verification_failed', "
            "'cancelled_keep_hold', 'approval_not_possible', 'manual')",
            name=f"ck_{t}_hold_reason_valid",
        ),
        sa.CheckConstraint(
            "trn_status IN ('none', 'not_verified', 'verified', 'invalid')",
            name=f"ck_{t}_trn_status_valid",
        ),
        sa.CheckConstraint("country ~ '^[A-Z]{2}$'", name=f"ck_{t}_country_format"),
        sa.CheckConstraint("trn IS NULL OR trn ~ '^[0-9]{15}$'", name=f"ck_{t}_trn_format"),
        sa.CheckConstraint(
            "phone_bidx IS NULL OR phone_bidx ~ '^[0-9a-f]{64}$'",
            name=f"ck_{t}_phone_bidx_format",
        ),
        sa.CheckConstraint(
            "payment_hold OR hold_reason IS NULL", name=f"ck_{t}_hold_reason_needs_hold"
        ),
    )
    op.create_index(f"ix_{t}_org_id", t, ["org_id"])
    op.create_index("ix_pay_vendors_status", t, ["org_id", "status", "created_at"])
    op.create_index("ix_pay_vendors_trn", t, ["org_id", "trn"])
    op.create_index("ix_pay_vendors_phone_bidx", t, ["org_id", "phone_bidx"])


def _accounts() -> None:
    t = ACCOUNTS
    op.create_table(
        t,
        *_base(t),
        sa.Column("vendor_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("iban", sa.Text(), nullable=False),
        sa.Column("iban_bidx", sa.String(64), nullable=False),
        sa.Column("iban_last4", sa.String(4), nullable=False),
        sa.Column("country", sa.String(2), nullable=False),
        sa.Column("bank_code", sa.String(16), nullable=True),
        sa.Column("account_name", sa.Text(), nullable=False),
        sa.Column("bank_name", sa.String(160), nullable=True),
        sa.Column("swift", sa.String(11), nullable=True),
        sa.Column("created_by_type", sa.String(16), nullable=False),
        sa.Column("created_by_id", sa.String(128), nullable=False),
        _ts("created_at", nullable=False),
        _ts("activated_at"),
        _ts("retired_at"),
        sa.UniqueConstraint("org_id", "id", name="uq_pay_vendor_bank_accounts_org_id_id"),
        _org_fk(t, "vendor_id", VENDORS, "vendor"),
        sa.CheckConstraint(
            "status IN ('proposed', 'active', 'retired', 'rejected')", name=f"ck_{t}_status_valid"
        ),
        sa.CheckConstraint("iban_bidx ~ '^[0-9a-f]{64}$'", name=f"ck_{t}_iban_bidx_format"),
        sa.CheckConstraint("country ~ '^[A-Z]{2}$'", name=f"ck_{t}_country_format"),
    )
    op.create_index(f"ix_{t}_org_id", t, ["org_id"])
    op.create_index("ix_pay_vendor_bank_accounts_vendor", t, ["org_id", "vendor_id", "created_at"])
    op.create_index("ix_pay_vendor_bank_accounts_iban_bidx", t, ["org_id", "iban_bidx"])
    op.create_index(
        "uq_pay_vendor_bank_accounts_one_active",
        t,
        ["org_id", "vendor_id"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )
    op.execute(
        """
        CREATE FUNCTION pay_bank_account_guard() RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = pg_catalog, public
        AS $$
        BEGIN
            IF NEW.org_id IS DISTINCT FROM OLD.org_id
               OR NEW.vendor_id IS DISTINCT FROM OLD.vendor_id
               OR NEW.iban_last4 IS DISTINCT FROM OLD.iban_last4
               OR NEW.country IS DISTINCT FROM OLD.country
               OR NEW.bank_code IS DISTINCT FROM OLD.bank_code
               OR NEW.bank_name IS DISTINCT FROM OLD.bank_name
               OR NEW.swift IS DISTINCT FROM OLD.swift
               OR NEW.created_by_type IS DISTINCT FROM OLD.created_by_type
               OR NEW.created_by_id IS DISTINCT FROM OLD.created_by_id
               OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
                RAISE EXCEPTION 'bank account details are locked: open a change ticket'
                    USING ERRCODE = 'check_violation';
            END IF;
            IF NEW.status IS DISTINCT FROM OLD.status AND NOT (
                (OLD.status = 'proposed' AND NEW.status IN ('active', 'rejected'))
                OR (OLD.status = 'active' AND NEW.status = 'retired')
            ) THEN
                RAISE EXCEPTION 'bank account status cannot move from % to %',
                    OLD.status, NEW.status USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END
        $$
        """
    )
    op.execute(
        f"CREATE TRIGGER {t}_guard BEFORE UPDATE ON {t} "
        "FOR EACH ROW EXECUTE FUNCTION pay_bank_account_guard()"
    )


def _tickets() -> None:
    t = TICKETS
    op.create_table(
        t,
        *_base(t),
        sa.Column("reference", sa.String(16), nullable=False),
        sa.Column("vendor_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("requested_by_type", sa.String(16), nullable=False),
        sa.Column("requested_by_id", sa.String(128), nullable=False),
        _user(t, "requester_user_id"),
        sa.Column("previous_account_id", sa.Uuid(), nullable=True),
        sa.Column("proposed_account_id", sa.Uuid(), nullable=False),
        sa.Column("details_digest", sa.String(64), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("request_claims", sa.Text(), nullable=True),
        sa.Column("verify_phone", sa.Text(), nullable=False),
        sa.Column("verify_phone_bidx", sa.String(64), nullable=True),
        sa.Column("phone_recently_changed", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("cooling_off_hours", sa.Integer(), nullable=False),
        _user(t, "verified_by"),
        _ts("verified_at"),
        sa.Column("verification_call_id", sa.Uuid(), nullable=True),
        sa.Column(
            "run_id",
            sa.Uuid(),
            sa.ForeignKey(
                "workflow_runs.id", ondelete="RESTRICT", name=f"fk_{t}_run_id_workflow_runs"
            ),
            nullable=True,
        ),
        sa.Column(
            "approval_id",
            sa.Uuid(),
            sa.ForeignKey(
                "approvals.id", ondelete="RESTRICT", name=f"fk_{t}_approval_id_approvals"
            ),
            nullable=True,
        ),
        _jsonb_list("approver_ids"),
        _ts("approved_at"),
        _ts("activates_at"),
        _ts("completed_at"),
        _ts("closed_at"),
        sa.Column("closed_by_type", sa.String(16), nullable=True),
        sa.Column("closed_by_id", sa.String(128), nullable=True),
        sa.Column("outcome", sa.String(64), nullable=True),
        sa.Column("close_reason", sa.Text(), nullable=True),
        sa.Column("hold_kept", sa.Boolean(), nullable=True),
        _jsonb_list("decision_ids"),
        _ts("created_at", nullable=False),
        _ts("updated_at", nullable=False),
        sa.UniqueConstraint("org_id", "id", name="uq_pay_bank_change_tickets_org_id_id"),
        sa.UniqueConstraint(
            "org_id", "reference", name="uq_pay_bank_change_tickets_org_id_reference"
        ),
        _org_fk(t, "vendor_id", VENDORS, "vendor"),
        _org_fk(t, "previous_account_id", ACCOUNTS, "previous_account"),
        _org_fk(t, "proposed_account_id", ACCOUNTS, "proposed_account"),
        sa.CheckConstraint(
            "status IN ('awaiting_verification', 'awaiting_approval', 'cooling_off', "
            "'completed', 'rejected', 'cancelled', 'failed')",
            name=f"ck_{t}_status_valid",
        ),
        sa.CheckConstraint(
            "source IN ('staff', 'email', 'invoice', 'vendor_portal', 'onboarding')",
            name=f"ck_{t}_source_valid",
        ),
        sa.CheckConstraint(
            "cooling_off_hours >= 0 AND cooling_off_hours <= 720",
            name=f"ck_{t}_cooling_off_range",
        ),
        sa.CheckConstraint(
            "details_digest ~ '^[0-9a-f]{64}$'", name=f"ck_{t}_details_digest_format"
        ),
    )
    op.create_index(f"ix_{t}_org_id", t, ["org_id"])
    op.create_index("ix_pay_bank_change_tickets_status", t, ["org_id", "status", "created_at"])
    op.create_index("ix_pay_bank_change_tickets_vendor", t, ["org_id", "vendor_id", "created_at"])
    op.create_index(
        "uq_pay_bank_change_tickets_one_open",
        t,
        ["org_id", "vendor_id"],
        unique=True,
        postgresql_where=sa.text(f"status IN ({_OPEN})"),
    )


def _calls() -> None:
    t = CALLS
    op.create_table(
        t,
        *_base(t),
        sa.Column("ticket_id", sa.Uuid(), nullable=False),
        _user(t, "called_by", nullable=False),
        sa.Column("result", sa.String(16), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("number_last4", sa.String(4), nullable=False),
        _ts("created_at", nullable=False),
        _org_fk(t, "ticket_id", TICKETS, "ticket"),
        sa.CheckConstraint(
            "result IN ('confirmed', 'not_confirmed', 'no_answer')", name=f"ck_{t}_result_valid"
        ),
    )
    op.create_index(f"ix_{t}_org_id", t, ["org_id"])
    op.create_index("ix_pay_bank_change_calls_ticket", t, ["org_id", "ticket_id", "created_at"])


def _onboarding() -> None:
    t = INVITES
    op.create_table(
        t,
        *_base(t),
        sa.Column("company_hint", sa.String(200), nullable=True),
        sa.Column("email", sa.Text(), nullable=True),
        sa.Column("locale", sa.String(2), nullable=False),
        sa.Column("secret_hash", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        _ts("used_at"),
        _ts("revoked_at"),
        sa.Column("vendor_id", sa.Uuid(), nullable=True),
        _user(t, "created_by", nullable=False),
        _ts("created_at", nullable=False),
        sa.UniqueConstraint("org_id", "id", name="uq_pay_vendor_invites_org_id_id"),
        _org_fk(t, "vendor_id", VENDORS, "vendor"),
        sa.CheckConstraint("locale IN ('en', 'ar')", name=f"ck_{t}_locale_valid"),
        sa.CheckConstraint("secret_hash ~ '^[0-9a-f]{64}$'", name=f"ck_{t}_secret_hash_format"),
    )
    op.create_index(f"ix_{t}_org_id", t, ["org_id"])
    op.create_index("ix_pay_vendor_invites_created", t, ["org_id", "created_at"])

    t = SESSIONS
    op.create_table(
        t,
        *_base(t),
        sa.Column("invite_id", sa.Uuid(), nullable=False),
        sa.Column("secret_hash", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        _jsonb_list("document_ids"),
        _ts("submitted_at"),
        _ts("created_at", nullable=False),
        _org_fk(t, "invite_id", INVITES, "invite"),
        sa.CheckConstraint("secret_hash ~ '^[0-9a-f]{64}$'", name=f"ck_{t}_secret_hash_format"),
    )
    op.create_index(f"ix_{t}_org_id", t, ["org_id"])
    op.create_index("ix_pay_vendor_onboarding_sessions_invite", t, ["org_id", "invite_id"])


def _settings() -> None:
    t = SETTINGS
    op.create_table(
        t,
        *_base(t),
        sa.Column("cooling_off_hours", sa.Integer(), nullable=False),
        sa.Column("phone_change_window_days", sa.Integer(), nullable=False),
        sa.Column("updated_by_type", sa.String(16), nullable=False),
        sa.Column("updated_by_id", sa.String(128), nullable=False),
        _ts("updated_at", nullable=False),
        sa.UniqueConstraint("org_id", name="uq_pay_settings_org_id"),
        sa.CheckConstraint(
            "cooling_off_hours >= 0 AND cooling_off_hours <= 720",
            name=f"ck_{t}_cooling_off_range",
        ),
        sa.CheckConstraint(
            "phone_change_window_days >= 0 AND phone_change_window_days <= 365",
            name=f"ck_{t}_phone_window_range",
        ),
    )
    op.create_index(f"ix_{t}_org_id", t, ["org_id"])


def _due_finder(app_role: str) -> None:
    op.execute(
        f"CREATE POLICY {TICKETS}_due_scan ON {TICKETS} FOR SELECT TO CURRENT_USER "
        "USING (current_setting('app.pay_due_scan', true) = 'on')"
    )
    op.execute(
        """
        CREATE FUNCTION pay_due_bank_change_org_ids() RETURNS SETOF uuid
        LANGUAGE plpgsql VOLATILE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        DECLARE
            previous text := current_setting('app.pay_due_scan', true);
            ids uuid[];
        BEGIN
            PERFORM set_config('app.pay_due_scan', 'on', true);
            SELECT array_agg(DISTINCT t.org_id ORDER BY t.org_id) INTO ids
              FROM public.pay_bank_change_tickets t
             WHERE t.status = 'cooling_off'
               AND t.activates_at <= now();
            PERFORM set_config('app.pay_due_scan', coalesce(previous, ''), true);
            RETURN QUERY SELECT unnest(coalesce(ids, '{}'::uuid[]));
        END
        $$
        """
    )
    op.execute("REVOKE ALL ON FUNCTION pay_due_bank_change_org_ids() FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION pay_due_bank_change_org_ids() TO {app_role}")


def upgrade() -> None:
    app_role = app_db_role()
    _vendors()
    _accounts()
    _tickets()
    _calls()
    _onboarding()
    _settings()
    for table in (VENDORS, ACCOUNTS, TICKETS, INVITES, SESSIONS, SETTINGS):
        for stmt in org_rls_statements(table, app_role, privileges="SELECT, INSERT, UPDATE"):
            op.execute(stmt)
    for stmt in org_rls_statements(CALLS, app_role, privileges="SELECT, INSERT"):
        op.execute(stmt)
    _due_finder(app_role)


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS pay_due_bank_change_org_ids()")
    for table in _ALL:
        op.drop_table(table)
    op.execute("DROP FUNCTION IF EXISTS pay_bank_account_guard()")
