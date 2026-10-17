# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Invitations, and the two narrow paths that work before a user has a membership (WP0.8).

* ``invitations``: tenant table (RLS forced, standard tenant policy). The application
  role may SELECT and INSERT, and UPDATE only the outcome columns (accepted_*, revoked_*).
  No DELETE: a revoked or expired invitation stays as a record. Only the SHA-256 of the
  token is stored. ``invitations_terminal`` refuses any UPDATE of an invitation that is
  already accepted or revoked (single use, enforced by the database).

Two SECURITY DEFINER functions, owned by the migration (owner) role, are the only way the
application role can reach tenant rows without first naming an organisation. Each
returns the smallest possible answer:

* ``invitation_org_id(token_hash)``: the organisation of the PENDING (not accepted, not
  revoked, not expired) invitation with this token hash, else NULL. The API calls it
  with ``sha256(token)``; only then does it set ``app.org_id`` to that organisation and
  re-read the invitation under normal RLS. Guessing a hash is as hard as guessing the
  256-bit token, and a used, revoked or expired token returns NULL.
* ``my_organisations()``: id, name and slug of the active organisations in which the
  authenticated subject (``app.auth_subject``, set by the API from the verified JWT) has
  a membership. Nothing else about those organisations, and nothing about others.

RLS is forced on the owner too, so each function turns a transaction-local flag on for
its own query only, and one SELECT policy per table, limited to the owner role, admits
rows while that flag is on (the same pattern as ``audit_org_ids`` in 0002). The
application role cannot use those policies (they apply to the owner role only) and has
EXECUTE on the two functions alone.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-07
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from pairly_core.migration_helpers import CURRENT_ORG_FN, app_db_role, org_rls_statements

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    app_role = app_db_role()

    op.create_table(
        "invitations",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "org_id",
            sa.Uuid(),
            sa.ForeignKey(
                "organisations.id", ondelete="RESTRICT", name="fk_invitations_org_id_organisations"
            ),
            nullable=False,
        ),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column(
            "role_id",
            sa.Uuid(),
            sa.ForeignKey("roles.id", ondelete="RESTRICT", name="fk_invitations_role_id_roles"),
            nullable=False,
        ),
        sa.Column("workflow", sa.String(32), nullable=True),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column(
            "invited_by",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="RESTRICT", name="fk_invitations_invited_by_users"),
            nullable=True,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "accepted_by",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="RESTRICT", name="fk_invitations_accepted_by_users"),
            nullable=True,
        ),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "revoked_by",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="RESTRICT", name="fk_invitations_revoked_by_users"),
            nullable=True,
        ),
        sa.UniqueConstraint("token_hash", name="uq_invitations_token_hash"),
        sa.CheckConstraint(
            "token_hash ~ '^[0-9a-f]{64}$'", name="ck_invitations_token_hash_format"
        ),
        sa.CheckConstraint(
            "workflow IS NULL OR workflow ~ '^[a-z][a-z0-9_]{0,31}$'",
            name="ck_invitations_workflow_format",
        ),
        sa.CheckConstraint(
            "email = lower(email) AND email = btrim(email)", name="ck_invitations_email_normalised"
        ),
        sa.CheckConstraint("expires_at > created_at", name="ck_invitations_expiry_after_creation"),
        sa.CheckConstraint(
            "(accepted_at IS NULL) = (accepted_by IS NULL)", name="ck_invitations_accepted_pair"
        ),
        sa.CheckConstraint(
            "accepted_at IS NULL OR revoked_at IS NULL", name="ck_invitations_single_outcome"
        ),
    )
    op.create_index("ix_invitations_org_id", "invitations", ["org_id"])
    op.create_index("ix_invitations_org_id_email", "invitations", ["org_id", "email"])

    # ---------------------------------------------------------------- privileges and RLS
    for stmt in org_rls_statements("invitations", app_role, privileges="SELECT, INSERT"):
        op.execute(stmt)
    op.execute(
        "GRANT UPDATE (accepted_at, accepted_by, revoked_at, revoked_by) "
        f"ON invitations TO {app_role}"
    )
    # Same rule as memberships: the role must be built in or one of this org's own roles.
    op.execute(
        "CREATE POLICY invitations_role_in_scope ON invitations AS RESTRICTIVE "  # noqa: S608  # nosec B608
        "FOR INSERT WITH CHECK (EXISTS (SELECT 1 FROM roles r WHERE r.id = role_id "
        f"AND (r.org_id IS NULL OR r.org_id = {CURRENT_ORG_FN})))"
    )

    # ---------------------------------------------------------------- single use
    op.execute(
        """
        CREATE FUNCTION invitations_reject_terminal_change() RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = pg_catalog, public
        AS $$
        BEGIN
            IF OLD.accepted_at IS NOT NULL OR OLD.revoked_at IS NOT NULL THEN
                RAISE EXCEPTION 'invitation is already accepted or revoked'
                    USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER invitations_terminal BEFORE UPDATE ON invitations "
        "FOR EACH ROW EXECUTE FUNCTION invitations_reject_terminal_change()"
    )
    op.execute("ALTER TABLE invitations ENABLE ALWAYS TRIGGER invitations_terminal")
    op.execute("REVOKE ALL ON FUNCTION invitations_reject_terminal_change() FROM PUBLIC")

    # ---------------------------------------------------------------- token lookup
    op.execute(
        "CREATE POLICY invitations_token_lookup ON invitations FOR SELECT TO CURRENT_USER "
        "USING (current_setting('app.invitation_lookup', true) = 'on')"
    )
    op.execute(
        """
        CREATE FUNCTION invitation_org_id(p_token_hash text) RETURNS uuid
        LANGUAGE plpgsql VOLATILE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        DECLARE
            previous text := current_setting('app.invitation_lookup', true);
            found uuid;
        BEGIN
            IF p_token_hash IS NULL OR p_token_hash !~ '^[0-9a-f]{64}$' THEN
                RETURN NULL;
            END IF;
            PERFORM set_config('app.invitation_lookup', 'on', true);
            SELECT i.org_id INTO found
              FROM public.invitations i
             WHERE i.token_hash = p_token_hash
               AND i.accepted_at IS NULL
               AND i.revoked_at IS NULL
               AND i.expires_at > now();
            PERFORM set_config('app.invitation_lookup', coalesce(previous, ''), true);
            RETURN found;
        END
        $$
        """
    )
    op.execute("REVOKE ALL ON FUNCTION invitation_org_id(text) FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION invitation_org_id(text) TO {app_role}")

    # ---------------------------------------------------------------- the subject's orgs
    # users needs no extra policy: users_select already shows the subject's own row.
    op.execute(
        "CREATE POLICY memberships_subject_lookup ON memberships FOR SELECT TO CURRENT_USER "
        "USING (current_setting('app.membership_lookup', true) = 'on')"
    )
    op.execute(
        "CREATE POLICY organisations_subject_lookup ON organisations FOR SELECT TO CURRENT_USER "
        "USING (current_setting('app.membership_lookup', true) = 'on')"
    )
    op.execute(
        """
        CREATE FUNCTION my_organisations()
        RETURNS TABLE (org_id uuid, org_name text, org_slug text)
        LANGUAGE plpgsql VOLATILE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        DECLARE
            previous text := current_setting('app.membership_lookup', true);
            subject text := public.app_current_subject();
        BEGIN
            IF subject IS NULL THEN
                RETURN;
            END IF;
            PERFORM set_config('app.membership_lookup', 'on', true);
            RETURN QUERY
                SELECT o.id, o.name::text, o.slug::text
                  FROM public.organisations o
                 WHERE o.status = 'active'
                   AND EXISTS (
                       SELECT 1
                         FROM public.memberships m
                         JOIN public.users u ON u.id = m.user_id
                        WHERE m.org_id = o.id
                          AND u.auth_subject = subject
                   )
                 ORDER BY o.name, o.id;
            PERFORM set_config('app.membership_lookup', coalesce(previous, ''), true);
        END
        $$
        """
    )
    op.execute("REVOKE ALL ON FUNCTION my_organisations() FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION my_organisations() TO {app_role}")


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS my_organisations()")
    op.execute("DROP POLICY IF EXISTS organisations_subject_lookup ON organisations")
    op.execute("DROP POLICY IF EXISTS memberships_subject_lookup ON memberships")
    op.execute("DROP FUNCTION IF EXISTS invitation_org_id(text)")
    op.drop_table("invitations")
    op.execute("DROP FUNCTION IF EXISTS invitations_reject_terminal_change()")
