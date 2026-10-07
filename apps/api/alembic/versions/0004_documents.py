# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Documents and inbound emails (WP1.1, WP1.2).

* ``inbound_emails`` and ``documents``: tenant tables, RLS forced with the standard
  tenant policy. The application role may SELECT, INSERT and UPDATE; there is no
  DELETE grant (retention deletion comes later as an audited job). ``documents`` is
  unique on ``(org_id, sha256)`` (dedupe), ``inbound_emails`` on ``(org_id, message_id)``.

Two SECURITY DEFINER functions, owned by the migration role, follow the pattern of
``audit_org_ids`` (0002): each turns a transaction-local flag on for its own query, and
one SELECT policy limited to the owner role admits rows while the flag is on. The
application role has EXECUTE on the functions only.

* ``inbound_org_id(slug)``: the id of the ACTIVE organisation with this slug, else
  NULL. Inbound email is addressed ``<workflow>+<slug>@<domain>``, so the worker must map
  a slug to an organisation before it has a tenant context. It returns the id only.
* ``documents_needing_work(p_limit)``: ``(org_id, id, status)`` of documents that
  stalled: ``received`` for over a minute (scan never queued), ``scanning`` with a
  retryable error and no attempt for 10 minutes, ``clean`` but not parsed for 10
  minutes. The worker's sweeper re-queues them. Ids and status only, nothing else.

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-07
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from pairly_core.migration_helpers import app_db_role, org_rls_statements

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_STATUSES = "'received', 'scanning', 'clean', 'infected', 'rejected', 'parsed', 'parse_failed'"
_SOURCES = "'upload', 'email', 'api', 'form'"
_WORKFLOW_CHECK = "workflow ~ '^[a-z][a-z0-9_]{0,31}$'"


def _now() -> sa.TextClause:
    return sa.text("now()")


def upgrade() -> None:
    app_role = app_db_role()

    op.create_table(
        "inbound_emails",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "org_id",
            sa.Uuid(),
            sa.ForeignKey(
                "organisations.id",
                ondelete="RESTRICT",
                name="fk_inbound_emails_org_id_organisations",
            ),
            nullable=False,
        ),
        sa.Column("workflow", sa.String(32), nullable=False),
        sa.Column("message_id", sa.String(998), nullable=False),
        sa.Column("raw_sha256", sa.String(64), nullable=False),
        sa.Column("sender_address", sa.String(320), nullable=True),
        sa.Column("sender_domain", sa.String(253), nullable=True),
        sa.Column("subject", sa.Text(), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "forwarded_chain",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("is_bulk", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("attachment_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=_now()),
        sa.UniqueConstraint("org_id", "message_id", name="uq_inbound_emails_org_id_message_id"),
        sa.CheckConstraint(_WORKFLOW_CHECK, name="ck_inbound_emails_workflow_format"),
        sa.CheckConstraint(
            "raw_sha256 ~ '^[0-9a-f]{64}$'", name="ck_inbound_emails_raw_sha256_format"
        ),
    )
    op.create_index("ix_inbound_emails_org_id", "inbound_emails", ["org_id"])

    op.create_table(
        "documents",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "org_id",
            sa.Uuid(),
            sa.ForeignKey(
                "organisations.id", ondelete="RESTRICT", name="fk_documents_org_id_organisations"
            ),
            nullable=False,
        ),
        sa.Column("workflow", sa.String(32), nullable=False),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("original_filename", sa.String(255), nullable=True),
        sa.Column("media_type", sa.String(127), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("storage_key", sa.Text(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("status_reason", sa.String(64), nullable=True),
        sa.Column("needs_review", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "submitted_by",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="RESTRICT", name="fk_documents_submitted_by_users"),
            nullable=True,
        ),
        sa.Column(
            "inbound_email_id",
            sa.Uuid(),
            sa.ForeignKey(
                "inbound_emails.id",
                ondelete="RESTRICT",
                name="fk_documents_inbound_email_id_inbound_emails",
            ),
            nullable=True,
        ),
        sa.Column("scan_attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_scan_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("scanned_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("scan_engine", sa.String(128), nullable=True),
        sa.Column("scan_signature", sa.String(255), nullable=True),
        sa.Column("parsed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("parser", sa.String(64), nullable=True),
        sa.Column("parser_version", sa.String(64), nullable=True),
        sa.Column("page_count", sa.Integer(), nullable=True),
        sa.Column("parse_result_key", sa.Text(), nullable=True),
        sa.Column(
            "parse_warnings",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=_now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=_now()),
        sa.UniqueConstraint("org_id", "sha256", name="uq_documents_org_id_sha256"),
        sa.CheckConstraint(f"status IN ({_STATUSES})", name="ck_documents_status_valid"),
        sa.CheckConstraint(f"source IN ({_SOURCES})", name="ck_documents_source_valid"),
        sa.CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="ck_documents_sha256_format"),
        sa.CheckConstraint(_WORKFLOW_CHECK, name="ck_documents_workflow_format"),
        sa.CheckConstraint("size_bytes >= 0", name="ck_documents_size_non_negative"),
    )
    op.create_index("ix_documents_org_id", "documents", ["org_id"])
    op.create_index("ix_documents_org_id_status", "documents", ["org_id", "status"])
    op.create_index("ix_documents_inbound_email_id", "documents", ["inbound_email_id"])
    op.create_index(
        "ix_documents_needs_review",
        "documents",
        ["org_id", "created_at"],
        postgresql_where=sa.text("needs_review"),
    )

    for table in ("inbound_emails", "documents"):
        for stmt in org_rls_statements(table, app_role, privileges="SELECT, INSERT, UPDATE"):
            op.execute(stmt)

    # ---------------------------------------------------------------- slug lookup
    op.execute(
        "CREATE POLICY organisations_inbound_lookup ON organisations FOR SELECT TO CURRENT_USER "
        "USING (current_setting('app.inbound_lookup', true) = 'on')"
    )
    op.execute(
        """
        CREATE FUNCTION inbound_org_id(p_slug text) RETURNS uuid
        LANGUAGE plpgsql VOLATILE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        DECLARE
            previous text := current_setting('app.inbound_lookup', true);
            found uuid;
        BEGIN
            IF p_slug IS NULL OR p_slug !~ '^[a-z0-9][a-z0-9-]{1,62}$' THEN
                RETURN NULL;
            END IF;
            PERFORM set_config('app.inbound_lookup', 'on', true);
            SELECT o.id INTO found
              FROM public.organisations o
             WHERE o.slug = p_slug AND o.status = 'active';
            PERFORM set_config('app.inbound_lookup', coalesce(previous, ''), true);
            RETURN found;
        END
        $$
        """
    )
    op.execute("REVOKE ALL ON FUNCTION inbound_org_id(text) FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION inbound_org_id(text) TO {app_role}")

    # ---------------------------------------------------------------- sweeper
    op.execute(
        "CREATE POLICY documents_sweep ON documents FOR SELECT TO CURRENT_USER "
        "USING (current_setting('app.document_sweep', true) = 'on')"
    )
    op.execute(
        """
        CREATE FUNCTION documents_needing_work(p_limit integer, p_max_scan_attempts integer)
        RETURNS TABLE (org_id uuid, document_id uuid, status text)
        LANGUAGE plpgsql VOLATILE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        DECLARE
            previous text := current_setting('app.document_sweep', true);
        BEGIN
            PERFORM set_config('app.document_sweep', 'on', true);
            RETURN QUERY
                SELECT d.org_id, d.id, d.status::text
                  FROM public.documents d
                 WHERE (d.status = 'received' AND d.created_at < now() - interval '1 minute')
                    OR (d.status = 'scanning'
                        AND d.scan_attempts < p_max_scan_attempts
                        AND coalesce(d.last_scan_at, d.created_at) < now() - interval '10 minutes')
                    OR (d.status = 'clean' AND d.updated_at < now() - interval '10 minutes')
                 ORDER BY d.updated_at
                 LIMIT least(greatest(p_limit, 1), 1000);
            PERFORM set_config('app.document_sweep', coalesce(previous, ''), true);
        END
        $$
        """
    )
    op.execute("REVOKE ALL ON FUNCTION documents_needing_work(integer, integer) FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION documents_needing_work(integer, integer) TO {app_role}")


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS documents_needing_work(integer, integer)")
    op.execute("DROP FUNCTION IF EXISTS inbound_org_id(text)")
    op.execute("DROP POLICY IF EXISTS organisations_inbound_lookup ON organisations")
    op.drop_table("documents")
    op.drop_table("inbound_emails")
