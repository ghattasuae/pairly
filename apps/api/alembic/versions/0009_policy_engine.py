# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Policy engine and hybrid retrieval (WP1.5).

Tenant tables (RLS forced, standard tenant policy, least privilege for the app role):

* ``rule_results``: every rule result and policy finding with its reason, facts,
  evidence and policy version. Append only: SELECT, INSERT, plus the
  ``workflow_reject_change`` trigger (0006) against UPDATE, DELETE and TRUNCATE.
* ``policy_documents``: uploaded policy document versions and their indexing state.
  SELECT, INSERT, and UPDATE on the state columns only. A trigger makes ``archived``
  final. No DELETE.
* ``search_chunks``: the generic hybrid search index (kind ``policy`` now, more kinds
  with WP1.12). Two generated ``tsvector`` columns (``english``, ``simple``), a
  ``vector(1536)`` embedding with an HNSW cosine index. SELECT, INSERT, DELETE (derived
  data, rebuilt on re-index); no UPDATE.

The ``vector`` extension is created by ``infra/postgres/init`` (superuser); the
``CREATE EXTENSION IF NOT EXISTS`` here only covers databases set up another way.

Revision ID: 0009
Revises: 0006
Create Date: 2026-10-07
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from pairly_core.migration_helpers import app_db_role, ident, org_rls_statements

revision: str = "0009"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_HEX64 = "'^[0-9a-f]{64}$'"
_NAME_RE = "'^[a-z][a-z0-9_.]{0,127}$'"


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


def _created() -> sa.Column[Any]:
    return sa.Column(
        "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
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
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    # ---------------------------------------------------------------- rule_results
    op.create_table(
        "rule_results",
        _pk(),
        _org("rule_results"),
        sa.Column("evaluation_id", sa.Uuid(), nullable=False),
        sa.Column("source", sa.String(8), nullable=False),
        sa.Column(
            "policy_version_id",
            sa.Uuid(),
            sa.ForeignKey(
                "policy_versions.id",
                ondelete="RESTRICT",
                name="fk_rule_results_policy_version_id_policy_versions",
            ),
            nullable=True,
        ),
        sa.Column("policy_name", sa.String(128), nullable=False),
        sa.Column("policy_version", sa.Integer(), nullable=True),
        sa.Column("rule_id", sa.String(64), nullable=False),
        sa.Column("outcome", sa.String(16), nullable=False),
        sa.Column("severity", sa.String(16), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("reason_ar", sa.Text(), nullable=True),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column(
            "facts", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column(
            "evidence", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")
        ),
        sa.Column("model_call_id", sa.Uuid(), nullable=True),
        sa.Column("as_of", sa.String(10), nullable=True),
        sa.Column("workflow", sa.String(32), nullable=True),
        sa.Column("subject_type", sa.String(64), nullable=True),
        sa.Column("subject_id", sa.String(128), nullable=True),
        sa.Column("created_by_type", sa.String(16), nullable=False),
        sa.Column("created_by_id", sa.String(128), nullable=False),
        _created(),
        sa.CheckConstraint("source IN ('rules', 'rag')", name="ck_rule_results_source_valid"),
        sa.CheckConstraint(
            "outcome IN ('pass', 'fail', 'needs_review', 'error', 'no_policy_found')",
            name="ck_rule_results_outcome_valid",
        ),
        sa.CheckConstraint(
            "source <> 'rules' OR (policy_version_id IS NOT NULL AND policy_version IS NOT NULL)",
            name="ck_rule_results_rules_have_version",
        ),
        sa.CheckConstraint(
            "created_by_type IN ('user', 'agent', 'system')", name="ck_rule_results_creator_valid"
        ),
    )
    op.create_index("ix_rule_results_org_id", "rule_results", ["org_id"])
    op.create_index("ix_rule_results_evaluation", "rule_results", ["org_id", "evaluation_id"])
    op.create_index(
        "ix_rule_results_subject", "rule_results", ["org_id", "subject_type", "subject_id"]
    )
    for stmt in org_rls_statements("rule_results", app_role, privileges="SELECT, INSERT"):
        op.execute(stmt)
    _immutable("rule_results")

    # ---------------------------------------------------------------- policy_documents
    op.create_table(
        "policy_documents",
        _pk(),
        _org("policy_documents"),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column(
            "document_id",
            sa.Uuid(),
            sa.ForeignKey(
                "documents.id",
                ondelete="RESTRICT",
                name="fk_policy_documents_document_id_documents",
            ),
            nullable=False,
        ),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("status_reason", sa.String(64), nullable=True),
        sa.Column("chunk_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("embedding_model", sa.String(128), nullable=True),
        sa.Column("embedding_version", sa.String(64), nullable=True),
        sa.Column("created_by_type", sa.String(16), nullable=False),
        sa.Column("created_by_id", sa.String(128), nullable=False),
        sa.Column("indexed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        _created(),
        sa.UniqueConstraint("org_id", "name", "version", name="uq_policy_documents_version"),
        sa.CheckConstraint("version >= 1", name="ck_policy_documents_version_positive"),
        sa.CheckConstraint(f"name ~ {_NAME_RE}", name="ck_policy_documents_name_format"),
        sa.CheckConstraint(
            "status IN ('pending', 'indexed', 'failed', 'archived')",
            name="ck_policy_documents_status_valid",
        ),
        sa.CheckConstraint(
            "created_by_type IN ('user', 'agent', 'system')",
            name="ck_policy_documents_creator_valid",
        ),
    )
    op.create_index("ix_policy_documents_org_id", "policy_documents", ["org_id"])
    op.create_index("ix_policy_documents_document_id", "policy_documents", ["document_id"])
    for stmt in org_rls_statements("policy_documents", app_role, privileges="SELECT, INSERT"):
        op.execute(stmt)
    role = ident(app_role)
    op.execute(
        "GRANT UPDATE (status, status_reason, chunk_count, embedding_model, "
        f"embedding_version, indexed_at, archived_at) ON policy_documents TO {role}"
    )
    op.execute(
        """
        CREATE FUNCTION policy_documents_guard() RETURNS trigger
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
    op.execute("REVOKE ALL ON FUNCTION policy_documents_guard() FROM PUBLIC")
    op.execute(
        "CREATE TRIGGER policy_documents_guard BEFORE UPDATE OR DELETE ON policy_documents "
        "FOR EACH ROW EXECUTE FUNCTION policy_documents_guard()"
    )
    op.execute("ALTER TABLE policy_documents ENABLE ALWAYS TRIGGER policy_documents_guard")

    # ---------------------------------------------------------------- search_chunks
    op.create_table(
        "search_chunks",
        _pk(),
        _org("search_chunks"),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("source_id", sa.Uuid(), nullable=False),
        sa.Column("source_version", sa.Integer(), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("page_start", sa.Integer(), nullable=True),
        sa.Column("page_end", sa.Integer(), nullable=True),
        sa.Column("section", sa.String(500), nullable=True),
        sa.Column(
            "meta", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column(
            "tsv_en",
            postgresql.TSVECTOR(),
            sa.Computed("to_tsvector('english'::regconfig, text)", persisted=True),
        ),
        sa.Column(
            "tsv_simple",
            postgresql.TSVECTOR(),
            sa.Computed("to_tsvector('simple'::regconfig, text)", persisted=True),
        ),
        sa.Column("embedding_model", sa.String(128), nullable=True),
        sa.Column("embedding_version", sa.String(64), nullable=True),
        _created(),
        sa.UniqueConstraint(
            "org_id", "kind", "source_id", "chunk_index", name="uq_search_chunks_position"
        ),
        sa.CheckConstraint("kind ~ '^[a-z][a-z0-9_]{0,31}$'", name="ck_search_chunks_kind_format"),
        sa.CheckConstraint("chunk_index >= 0", name="ck_search_chunks_chunk_index_non_negative"),
        sa.CheckConstraint(
            "char_length(text) BETWEEN 1 AND 4000", name="ck_search_chunks_text_length"
        ),
        sa.CheckConstraint(f"content_hash ~ {_HEX64}", name="ck_search_chunks_content_hash_format"),
    )
    op.execute("ALTER TABLE search_chunks ADD COLUMN embedding vector(1536)")
    op.execute(
        "ALTER TABLE search_chunks ADD CONSTRAINT ck_search_chunks_embedding_has_model "
        "CHECK ((embedding IS NULL) = (embedding_model IS NULL))"
    )
    op.create_index("ix_search_chunks_org_id", "search_chunks", ["org_id"])
    op.create_index("ix_search_chunks_source", "search_chunks", ["org_id", "kind", "source_id"])
    op.execute("CREATE INDEX ix_search_chunks_tsv_en ON search_chunks USING gin (tsv_en)")
    op.execute("CREATE INDEX ix_search_chunks_tsv_simple ON search_chunks USING gin (tsv_simple)")
    op.execute(
        "CREATE INDEX ix_search_chunks_embedding ON search_chunks "
        "USING hnsw (embedding vector_cosine_ops)"
    )
    for stmt in org_rls_statements("search_chunks", app_role, privileges="SELECT, INSERT, DELETE"):
        op.execute(stmt)


def downgrade() -> None:
    op.drop_table("search_chunks")
    op.drop_table("policy_documents")
    op.execute("DROP FUNCTION IF EXISTS policy_documents_guard()")
    op.drop_table("rule_results")
