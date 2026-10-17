# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""``demo_signin_org_id(slug)``: the hosted demo's public sign-in finds its organisation.

The public demo sign-in (``pairly_core.demo_signin``, ``GET /v1/demo/signin-accounts``)
answers without a signed-in person, so the application role has no tenant to read the
organisation or its feature flags with. This adds, for that purpose only:

* read policies on ``organisations`` and ``feature_flags`` for the owner role (``TO
  CURRENT_USER``, the role running this migration), switched on per transaction by
  ``app.demo_signin_lookup``. The setting means nothing to the application role,
  because the policies do not apply to it;
* the SECURITY DEFINER function ``demo_signin_org_id(text)``: the id of the active
  organisation with that slug, and only when that organisation turned the
  ``demo.public_signin`` flag on (the flag is off by default, so an organisation that
  never turned it on is never found). Otherwise NULL. It returns the id and nothing
  else. EXECUTE is revoked from PUBLIC and granted to the application role.

The caller then binds the session to that organisation and reads its members under Row
Level Security, as the careers routes do.

Revision ID: 0034
Revises: 0033
Create Date: 2026-10-17
"""

from collections.abc import Sequence

from alembic import op

from pairly_core.migration_helpers import app_db_role

revision: str = "0034"
down_revision: str | None = "0033"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SETTING = "app.demo_signin_lookup"


def upgrade() -> None:
    app_role = app_db_role()
    op.execute(
        "CREATE POLICY organisations_demo_signin_lookup ON organisations FOR SELECT "
        f"TO CURRENT_USER USING (current_setting('{_SETTING}', true) = 'on')"
    )
    op.execute(
        "CREATE POLICY feature_flags_demo_signin_lookup ON feature_flags FOR SELECT "
        f"TO CURRENT_USER USING (current_setting('{_SETTING}', true) = 'on')"
    )
    op.execute(
        """
        CREATE FUNCTION demo_signin_org_id(p_slug text)
        RETURNS uuid
        LANGUAGE plpgsql VOLATILE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        DECLARE
            previous text := current_setting('app.demo_signin_lookup', true);
            found uuid;
        BEGIN
            IF p_slug IS NULL OR p_slug !~ '^[a-z0-9][a-z0-9-]{1,62}$' THEN
                RETURN NULL;
            END IF;
            PERFORM set_config('app.demo_signin_lookup', 'on', true);
            SELECT o.id INTO found
              FROM public.organisations o
             WHERE o.slug = p_slug
               AND o.status = 'active'
               AND EXISTS (
                   SELECT 1
                     FROM public.feature_flags f
                    WHERE f.org_id = o.id
                      AND f.key = 'demo.public_signin'
                      AND f.enabled
               );
            PERFORM set_config('app.demo_signin_lookup', coalesce(previous, ''), true);
            RETURN found;
        END
        $$
        """
    )
    op.execute("REVOKE ALL ON FUNCTION demo_signin_org_id(text) FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION demo_signin_org_id(text) TO {app_role}")


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS demo_signin_org_id(text)")
    op.execute("DROP POLICY IF EXISTS feature_flags_demo_signin_lookup ON feature_flags")
    op.execute("DROP POLICY IF EXISTS organisations_demo_signin_lookup ON organisations")
