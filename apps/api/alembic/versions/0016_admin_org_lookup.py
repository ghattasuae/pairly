# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""``admin_org_id_by_slug(slug)``: the owner role finds an organisation by its slug.

Operator commands (``pairly-admin seed <name> --org <slug>``) name an organisation by
slug. ``organisations`` forces Row Level Security, so even the owner sees no row without
a tenant. This adds, for that purpose only:

* a read policy on ``organisations`` for the owner role (``TO CURRENT_USER``, the role
  running this migration), switched on per transaction by ``app.admin_slug_lookup``;
* the SECURITY DEFINER function ``admin_org_id_by_slug(text)`` that switches it on,
  reads the id of the organisation with that slug (or NULL), and switches it off. It
  returns the id only. EXECUTE is revoked from PUBLIC and granted to nobody else, so the
  application role cannot call it; the flag means nothing to the application role,
  because the policy does not apply to it.

Revision ID: 0016
Revises: 0015
Create Date: 2026-10-08
"""

from collections.abc import Sequence

from alembic import op

from pairly_core.migration_helpers import app_db_role

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    app_role = app_db_role()
    op.execute(
        "CREATE POLICY organisations_admin_slug_lookup ON organisations FOR SELECT "
        "TO CURRENT_USER USING (current_setting('app.admin_slug_lookup', true) = 'on')"
    )
    op.execute(
        """
        CREATE FUNCTION admin_org_id_by_slug(p_slug text)
        RETURNS uuid
        LANGUAGE plpgsql VOLATILE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        DECLARE
            previous text := current_setting('app.admin_slug_lookup', true);
            found uuid;
        BEGIN
            IF p_slug IS NULL OR p_slug !~ '^[a-z0-9][a-z0-9-]{1,62}$' THEN
                RETURN NULL;
            END IF;
            PERFORM set_config('app.admin_slug_lookup', 'on', true);
            SELECT o.id INTO found FROM public.organisations o WHERE o.slug = p_slug;
            PERFORM set_config('app.admin_slug_lookup', coalesce(previous, ''), true);
            RETURN found;
        END
        $$
        """
    )
    op.execute("REVOKE ALL ON FUNCTION admin_org_id_by_slug(text) FROM PUBLIC")
    op.execute(f"REVOKE ALL ON FUNCTION admin_org_id_by_slug(text) FROM {app_role}")


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS admin_org_id_by_slug(text)")
    op.execute("DROP POLICY IF EXISTS organisations_admin_slug_lookup ON organisations")
