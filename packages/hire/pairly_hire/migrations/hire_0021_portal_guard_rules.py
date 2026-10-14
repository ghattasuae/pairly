# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Pairly Hire: the candidate portal's narrow exceptions to the core portal guard.

Core migration 0022 closes every org-scoped core table to portal sessions. The candidate
portal (``hire_candidate:<uuid>``, ``hire_portal_candidate()``) legitimately needs a few
core rows, each tied to the candidate:

* ``audit_events``: insert the events the candidate is the actor of; never read.
* ``decision_records``: read the decisions about their own applications and CVs (the
  privacy centre, receipts, appeals) and the ones they made themselves (a withdrawal);
  insert only as themselves.
* ``appeals``: read and open their own (``requester_id`` ``hire_candidate:<id>``).
* ``documents``: besides their own uploads (core), the CVs of their own applications
  and the files of their own document requests.
* ``policy_versions``: the rubric versions of the decisions they can see (a receipt's
  criteria), read only.

Hire tables without a portal policy of their own get the guard too:
``hire_jobs`` and ``hire_pipeline_configs`` show only the jobs (and their stages, which
the portal maps to honest steps) of the candidate's applications, read only;
``hire_org_settings`` and ``hire_messaging_settings`` show nothing.

Revision ID: hire_0021
Revises: hire_0020
Create Date: 2026-10-14
"""

from collections.abc import Sequence

from alembic import op

from pairly_core.migration_helpers import (
    drop_portal_rule_statements,
    portal_guard_statements,
    portal_rule_statements,
)

revision: str = "hire_0021"
down_revision: str | None = "hire_0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = "0022"

KIND = "hire_candidate"
_ME = "app_portal_id() = hire_portal_candidate()"
# The candidate as actor: their id, bare or as the portal subject (an offer answer).
_ACTOR = (
    f"{_ME} AND actor_type = 'candidate' AND actor_id IN "
    "(hire_portal_candidate()::text, 'hire_candidate:' || hire_portal_candidate()::text)"
)
_OWN_APPS = "SELECT a.id FROM hire_applications a WHERE a.candidate_id = hire_portal_candidate()"
_OWN_JOBS = (
    "SELECT a.job_id FROM hire_applications a WHERE a.candidate_id = hire_portal_candidate()"
)
_OWN_CVS = (
    "SELECT a.cv_document_id FROM hire_applications a "
    "WHERE a.candidate_id = hire_portal_candidate() AND a.cv_document_id IS NOT NULL"
)
_ABOUT_ME = (
    f"{_ME} AND workflow = 'hire' AND ("  # noqa: S608 (constant SQL, never user input)
    f"(entity_type = 'hire.application' AND entity_id IN (SELECT x.id::text FROM ({_OWN_APPS}) x))"
    f" OR (entity_type = 'document' AND entity_id IN "
    f"(SELECT x.cv_document_id::text FROM ({_OWN_CVS}) x)))"
)
_REQUESTER = (
    f"{_ME} AND requester_type = 'candidate' "
    "AND requester_id = 'hire_candidate:' || hire_portal_candidate()::text"
)
_DOCUMENTS = (
    f"{_ME} AND (id IN ({_OWN_CVS}) OR id IN (SELECT r.document_id FROM "  # noqa: S608 (constant SQL, never user input)
    "hire_document_requests r WHERE r.candidate_id = hire_portal_candidate() "
    "AND r.document_id IS NOT NULL))"
)

RULES: tuple[tuple[str, str | None, str | None], ...] = (
    ("audit_events", None, _ACTOR),
    ("decision_records", f"({_ABOUT_ME}) OR ({_ACTOR})", _ACTOR),
    ("appeals", _REQUESTER, _REQUESTER),
    ("documents", _DOCUMENTS, None),
    (
        "policy_versions",
        f"{_ME} AND id IN (SELECT d.rubric_version_id FROM decision_records d "  # noqa: S608 (constant SQL, never user input)
        "WHERE d.rubric_version_id IS NOT NULL)",
        None,
    ),
    ("hire_jobs", f"{_ME} AND id IN ({_OWN_JOBS})", None),
    ("hire_pipeline_configs", f"{_ME} AND job_id IN ({_OWN_JOBS})", None),
)
GUARDED: tuple[str, ...] = (
    "hire_jobs",
    "hire_org_settings",
    "hire_messaging_settings",
    "hire_pipeline_configs",
)


def upgrade() -> None:
    for table in GUARDED:
        for stmt in portal_guard_statements(table):
            op.execute(stmt)
    for table, using, check in RULES:
        for stmt in portal_rule_statements(table, KIND, using=using, check=check):
            op.execute(stmt)


def downgrade() -> None:
    for table, _using, _check in reversed(RULES):
        for stmt in drop_portal_rule_statements(table, KIND):
            op.execute(stmt)
    for table in GUARDED:
        op.execute(f"DROP POLICY IF EXISTS {table}_portal_guard ON {table}")
