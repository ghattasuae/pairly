# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Guardrails for policy documents (WP1.4 into WP1.5).

* ``policy_documents.guardrail_findings`` (jsonb array): the guardrail findings of the
  document, shown to admins as warnings. ``guardrail_version``: the guardrails version
  that produced them. The application role may UPDATE both.
* Status ``needs_review``: a high-severity instruction finding (or a guardrail detector
  that failed). ``policy_documents_guard`` now refuses any change from ``needs_review``
  except to ``archived``, so such a document can never become active; an admin uploads a
  corrected file, which is a new version.

The downgrade turns ``needs_review`` rows into ``failed`` (reason
``guardrails_needs_review``), which keeps them inactive under 0009's rules.

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-07
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from pairly_core.migration_helpers import app_db_role, ident

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_STATUS_0009 = "status IN ('pending', 'indexed', 'failed', 'archived')"
_STATUS_0010 = "status IN ('pending', 'indexed', 'failed', 'needs_review', 'archived')"


def upgrade() -> None:
    role = ident(app_db_role())
    op.add_column(
        "policy_documents",
        sa.Column(
            "guardrail_findings",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.add_column("policy_documents", sa.Column("guardrail_version", sa.String(8), nullable=True))
    op.create_check_constraint(
        "ck_policy_documents_guardrail_findings_array",
        "policy_documents",
        "jsonb_typeof(guardrail_findings) = 'array'",
    )
    op.drop_constraint("ck_policy_documents_status_valid", "policy_documents", type_="check")
    op.create_check_constraint("ck_policy_documents_status_valid", "policy_documents", _STATUS_0010)
    op.execute(
        f"GRANT UPDATE (guardrail_findings, guardrail_version) ON policy_documents TO {role}"
    )
    op.execute(
        """
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
            IF OLD.status = 'needs_review' AND NEW.status NOT IN ('needs_review', 'archived') THEN
                RAISE EXCEPTION 'a policy document that needs review cannot be activated'
                    USING ERRCODE = 'insufficient_privilege';
            END IF;
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
    )


def downgrade() -> None:
    role = ident(app_db_role())
    op.execute(
        """
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
    )
    # RLS is forced on the owner too: lift it for this one statement's transaction.
    op.execute("ALTER TABLE policy_documents NO FORCE ROW LEVEL SECURITY")
    op.execute(
        "UPDATE policy_documents SET status = 'failed', "
        "status_reason = 'guardrails_needs_review' WHERE status = 'needs_review'"
    )
    op.execute("ALTER TABLE policy_documents FORCE ROW LEVEL SECURITY")
    op.drop_constraint("ck_policy_documents_status_valid", "policy_documents", type_="check")
    op.create_check_constraint("ck_policy_documents_status_valid", "policy_documents", _STATUS_0009)
    op.execute(
        f"REVOKE UPDATE (guardrail_findings, guardrail_version) ON policy_documents FROM {role}"
    )
    op.drop_constraint(
        "ck_policy_documents_guardrail_findings_array", "policy_documents", type_="check"
    )
    op.drop_column("policy_documents", "guardrail_version")
    op.drop_column("policy_documents", "guardrail_findings")
