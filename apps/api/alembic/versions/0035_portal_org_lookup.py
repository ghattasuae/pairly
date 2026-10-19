# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""``portal_org_id_by_slug(slug)``: a public sign-in form finds its organisation by slug.

The vendor portal's sign-in form (``POST /v1/pay/vendor-portal/login-link``) answers
without a signed-in person, so the application role has no tenant to read
``organisations`` with. The careers lookup (``hire_careers_org``, migration
``hire_0001``) only finds organisations whose careers page is visible, the demo lookup
(``demo_signin_org_id``, 0034) only the one that turned the public demo on, and the
operator lookup (``admin_org_id_by_slug``, 0016) is not granted to the application role.
This adds, for that purpose only:

* a read policy on ``organisations`` for the owner role (``TO CURRENT_USER``, the role
  running this migration), switched on per transaction by ``app.portal_org_lookup``. The
  setting means nothing to the application role, because the policy does not apply to
  it;
* the SECURITY DEFINER function ``portal_org_id_by_slug(text)``: the id of the active
  organisation with that slug, otherwise NULL. It returns the id and nothing else.
  EXECUTE is revoked from PUBLIC and granted to the application role.

The caller answers the same whether or not the slug exists and binds the session to the
organisation it found before reading anything else, under Row Level Security.

Revision ID: 0035
Revises: 0034
Create Date: 2026-10-19
"""

from collections.abc import Sequence

from alembic import op

from pairly_core.migration_helpers import app_db_role

revision: str = "0035"
down_revision: str | None = "0034"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SETTING = "app.portal_org_lookup"


def upgrade() -> None:
    app_role = app_db_role()
    op.execute(
        "CREATE POLICY organisations_portal_org_lookup ON organisations FOR SELECT "
        f"TO CURRENT_USER USING (current_setting('{_SETTING}', true) = 'on')"
    )
    op.execute(
        """
        CREATE FUNCTION portal_org_id_by_slug(p_slug text)
        RETURNS uuid
        LANGUAGE plpgsql VOLATILE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        DECLARE
            previous text := current_setting('app.portal_org_lookup', true);
            found uuid;
        BEGIN
            IF p_slug IS NULL OR p_slug !~ '^[a-z0-9][a-z0-9-]{1,62}$' THEN
                RETURN NULL;
            END IF;
            PERFORM set_config('app.portal_org_lookup', 'on', true);
            SELECT o.id INTO found
              FROM public.organisations o
             WHERE o.slug = p_slug
               AND o.status = 'active';
            PERFORM set_config('app.portal_org_lookup', coalesce(previous, ''), true);
            RETURN found;
        END
        $$
        """
    )
    op.execute("REVOKE ALL ON FUNCTION portal_org_id_by_slug(text) FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION portal_org_id_by_slug(text) TO {app_role}")


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS portal_org_id_by_slug(text)")
    op.execute("DROP POLICY IF EXISTS organisations_portal_org_lookup ON organisations")
