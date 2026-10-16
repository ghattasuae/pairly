# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Pairly Hire: one document row per application (security review 2026-10, L-09).

Before core 0033, intake linked a CV whose bytes another candidate had already sent to
that candidate's ``documents`` row: the second candidate's portal showed the first
one's file name and metadata, and erasing either touched the other's file. Candidate
intake now stores every file with its own row (``dedupe = false``); this migration
splits the rows that applications already share.

In every organisation, for every document that two or more applications reference (the
CV, an application's other files, a document request's file), the earliest application
keeps the row and each other application gets a copy of it: the same stored bytes,
scan and parse results, with no file name (only the first sender's name was ever kept)
and no portal subject (the candidate reaches it through their own application). That
application's CV, files, document requests and screenings then point at the copy. Every
row involved becomes ``dedupe = false``.

Downgrade leaves the copies in place: they are valid rows, and putting applications
back on one row would undo the fix.

Revision ID: hire_0022
Revises: hire_0021
Create Date: 2026-10-16
"""

import uuid
from collections import defaultdict
from collections.abc import Iterable, Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "hire_0022"
down_revision: str | None = "hire_0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = "0033"

# Copied as they are; ``original_filename`` and ``portal_subject`` are left empty and
# ``dedupe`` is false.
_COPIED = (
    "org_id, workflow, source, media_type, size_bytes, sha256, storage_key, status, "
    "status_reason, needs_review, submitted_by, inbound_email_id, scan_attempts, "
    "last_scan_at, scanned_at, scan_engine, scan_signature, parsed_at, parser, "
    "parser_version, page_count, parse_result_key, parse_warnings, created_at, updated_at, "
    "erased_at, objects_erased_at"
)

# Every (document, application) reference of the session's organisation.
_REFERENCES = """
SELECT r.doc, r.app, a.created_at
FROM (
    SELECT cv_document_id AS doc, id AS app FROM hire_applications
    WHERE cv_document_id IS NOT NULL
    UNION
    SELECT f.value::uuid, a.id FROM hire_applications a, jsonb_each_text(a.files) f
    WHERE f.value ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
    UNION
    SELECT document_id, application_id FROM hire_document_requests
    WHERE document_id IS NOT NULL
) r
JOIN hire_applications a ON a.id = r.app
JOIN documents d ON d.id = r.doc
ORDER BY r.doc, a.created_at, r.app
"""


def _organisations(conn: sa.Connection) -> list[uuid.UUID]:
    conn.execute(sa.text("SELECT set_config('app.crypto_org_scan', 'on', true)"))
    try:
        rows = conn.execute(sa.text("SELECT id FROM organisations")).scalars()
        return [uuid.UUID(str(o)) for o in rows]
    finally:
        conn.execute(sa.text("SELECT set_config('app.crypto_org_scan', '', true)"))


def _split(conn: sa.Connection, doc: uuid.UUID, app: uuid.UUID) -> None:
    copy = uuid.uuid4()
    conn.execute(
        sa.text(
            f"INSERT INTO documents (id, original_filename, portal_subject, dedupe, {_COPIED}) "  # noqa: S608  # nosec B608
            f"SELECT :copy, NULL, NULL, false, {_COPIED} FROM documents WHERE id = :doc"
        ),
        {"copy": copy, "doc": doc},
    )
    params = {"copy": copy, "doc": doc, "app": app, "doc_text": str(doc), "copy_text": str(copy)}
    conn.execute(
        sa.text(
            "UPDATE hire_applications SET cv_document_id = :copy "
            "WHERE id = :app AND cv_document_id = :doc"
        ),
        params,
    )
    conn.execute(
        sa.text(
            "UPDATE hire_applications SET files = ("
            "SELECT coalesce(jsonb_object_agg(f.key, CASE WHEN f.value = :doc_text "
            "THEN to_jsonb(CAST(:copy_text AS text)) ELSE to_jsonb(f.value) END), '{}'::jsonb) "
            "FROM jsonb_each_text(files) f) "
            "WHERE id = :app AND files IS NOT NULL AND EXISTS ("
            "SELECT 1 FROM jsonb_each_text(files) g WHERE g.value = :doc_text)"
        ),
        params,
    )
    conn.execute(
        sa.text(
            "UPDATE hire_document_requests SET document_id = :copy "
            "WHERE application_id = :app AND document_id = :doc"
        ),
        params,
    )
    conn.execute(
        sa.text(
            "UPDATE hire_screenings SET document_id = :copy "
            "WHERE application_id = :app AND document_id = :doc"
        ),
        params,
    )


def split_shared(conn: sa.Connection, orgs: Iterable[uuid.UUID] | None = None) -> int:
    """Give every application that shares a document with an earlier one its own copy,
    in ``orgs`` (every organisation when ``None``). Returns how many copies it made."""
    copies = 0
    for org in _organisations(conn) if orgs is None else orgs:
        conn.execute(sa.text("SELECT set_config('app.org_id', :org, true)"), {"org": str(org)})
        apps_of: dict[uuid.UUID, list[uuid.UUID]] = defaultdict(list)
        for doc, app, _ in conn.execute(sa.text(_REFERENCES)):
            if app not in apps_of[doc]:
                apps_of[doc].append(app)
        shared = {doc: apps for doc, apps in apps_of.items() if len(apps) > 1}
        for doc, apps in shared.items():
            conn.execute(
                sa.text("UPDATE documents SET dedupe = false WHERE id = :doc"), {"doc": doc}
            )
            for app in apps[1:]:
                _split(conn, doc, app)
                copies += 1
        conn.execute(sa.text("SELECT set_config('app.org_id', '', true)"))
    return copies


def upgrade() -> None:
    split_shared(op.get_bind())


def downgrade() -> None:
    """Nothing to undo: the copies stay (see the module docstring)."""
