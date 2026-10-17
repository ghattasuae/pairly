# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Application-level field encryption (WP1.14).

* ``model_calls.request`` and ``model_calls.response``: jsonb becomes text holding an
  AES-256-GCM envelope token (``pairly_core.crypto``), bound to (org, table, column, id).
* ``inbound_emails.sender_address``: varchar becomes an encrypted token, plus
  ``sender_address_bidx`` (HMAC-SHA256 blind index of the normalised address, indexed
  with ``org_id``) for equality search and dedupe.
* ``crypto_org_ids()``: SECURITY DEFINER, owned by the migration (owner) role, returns
  every organisation id. ``pairly-admin crypto rotate`` (owner role) uses it to walk
  the tenants, the same pattern as ``audit_org_ids`` (0002): it turns
  ``app.crypto_org_scan`` on for its own query, and one SELECT policy on
  ``organisations``, limited to the owner role, admits rows while the flag is on. The
  application role gets no EXECUTE on it.

Existing rows are converted in place, so the migration needs the keys
(``PAIRLY_KEK_*``, ``PAIRLY_KEK_CURRENT``, ``PAIRLY_BLIND_INDEX_KEY``) in its
environment when either table holds rows. With empty tables it needs no keys. RLS is
forced on the owner too, so the conversion lifts FORCE for the two tables inside this
migration's transaction and puts it back before committing. The downgrade decrypts.

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-07
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _has_rows(conn: sa.Connection, table: str) -> bool:
    return conn.execute(sa.text(f"SELECT EXISTS (SELECT 1 FROM {table})")).scalar() is True  # noqa: S608  # nosec B608


def _keyring() -> Any:
    from pairly_core.crypto.keys import load_keyring

    return load_keyring()


def _convert_model_calls(conn: sa.Connection, *, encrypting: bool) -> None:
    import json

    from pairly_core.crypto.envelope import Aad, decrypt, encrypt

    if not _has_rows(conn, "model_calls"):
        return
    ring = _keyring()
    rows = conn.execute(
        sa.text(
            "SELECT id, org_id, request::text AS request, response::text AS response "
            "FROM model_calls"
        )
    ).all()
    for row in rows:
        values: dict[str, Any] = {"id": row.id}
        for column in ("request", "response"):
            value = getattr(row, column)
            aad = Aad.for_row(row.org_id, "model_calls", column, row.id)
            if value is None:
                values[column] = None
            elif encrypting:
                canonical = json.dumps(
                    json.loads(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")
                )
                values[column] = encrypt(canonical.encode("utf-8"), aad, ring)
            else:
                values[column] = decrypt(value, aad, ring).decode("utf-8")
        conn.execute(
            sa.text(
                "UPDATE model_calls SET request_new = :request, response_new = :response "
                "WHERE id = :id"
            ),
            values,
        )


def _convert_inbound_emails(conn: sa.Connection, *, encrypting: bool) -> None:
    from pairly_core.crypto.blind_index import blind_index
    from pairly_core.crypto.envelope import Aad, decrypt, encrypt

    if not _has_rows(conn, "inbound_emails"):
        return
    ring = _keyring()
    rows = conn.execute(
        sa.text(
            "SELECT id, org_id, sender_address FROM inbound_emails WHERE sender_address IS NOT NULL"
        )
    ).all()
    for row in rows:
        aad = Aad.for_row(row.org_id, "inbound_emails", "sender_address", row.id)
        if encrypting:
            params = {
                "id": row.id,
                "value": encrypt(row.sender_address.encode("utf-8"), aad, ring),
                "bidx": blind_index("email", row.sender_address, row.org_id, ring),
            }
            conn.execute(
                sa.text(
                    "UPDATE inbound_emails SET sender_address_new = :value, "
                    "sender_address_bidx = :bidx WHERE id = :id"
                ),
                params,
            )
        else:
            plain = decrypt(row.sender_address, aad, ring).decode("utf-8")
            conn.execute(
                sa.text("UPDATE inbound_emails SET sender_address_new = :value WHERE id = :id"),
                {"id": row.id, "value": plain},
            )


def upgrade() -> None:
    conn = op.get_bind()
    for table in ("model_calls", "inbound_emails"):
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")

    # ---------------------------------------------------------------- model_calls
    op.add_column("model_calls", sa.Column("request_new", sa.Text(), nullable=True))
    op.add_column("model_calls", sa.Column("response_new", sa.Text(), nullable=True))
    _convert_model_calls(conn, encrypting=True)
    op.drop_column("model_calls", "request")
    op.drop_column("model_calls", "response")
    op.alter_column("model_calls", "request_new", new_column_name="request", nullable=False)
    op.alter_column("model_calls", "response_new", new_column_name="response")

    # ---------------------------------------------------------------- inbound_emails
    op.add_column("inbound_emails", sa.Column("sender_address_new", sa.Text(), nullable=True))
    op.add_column("inbound_emails", sa.Column("sender_address_bidx", sa.String(64), nullable=True))
    _convert_inbound_emails(conn, encrypting=True)
    op.drop_column("inbound_emails", "sender_address")
    op.alter_column("inbound_emails", "sender_address_new", new_column_name="sender_address")
    op.create_check_constraint(
        "ck_inbound_emails_sender_address_bidx_format",
        "inbound_emails",
        "sender_address_bidx IS NULL OR sender_address_bidx ~ '^[0-9a-f]{64}$'",
    )
    op.create_index(
        "ix_inbound_emails_org_id_sender_address_bidx",
        "inbound_emails",
        ["org_id", "sender_address_bidx"],
    )

    for table in ("model_calls", "inbound_emails"):
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")

    # ---------------------------------------------------------------- org enumeration
    op.execute(
        "CREATE POLICY organisations_crypto_scan ON organisations FOR SELECT TO CURRENT_USER "
        "USING (current_setting('app.crypto_org_scan', true) = 'on')"
    )
    op.execute(
        """
        CREATE FUNCTION crypto_org_ids() RETURNS SETOF uuid
        LANGUAGE plpgsql VOLATILE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        DECLARE
            previous text := current_setting('app.crypto_org_scan', true);
            ids uuid[];
        BEGIN
            PERFORM set_config('app.crypto_org_scan', 'on', true);
            SELECT array_agg(o.id ORDER BY o.id) INTO ids FROM public.organisations o;
            PERFORM set_config('app.crypto_org_scan', coalesce(previous, ''), true);
            RETURN QUERY SELECT unnest(coalesce(ids, '{}'::uuid[]));
        END
        $$
        """
    )
    op.execute("REVOKE ALL ON FUNCTION crypto_org_ids() FROM PUBLIC")


def downgrade() -> None:
    conn = op.get_bind()
    op.execute("DROP FUNCTION IF EXISTS crypto_org_ids()")
    op.execute("DROP POLICY IF EXISTS organisations_crypto_scan ON organisations")
    for table in ("model_calls", "inbound_emails"):
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")

    op.drop_index("ix_inbound_emails_org_id_sender_address_bidx", table_name="inbound_emails")
    op.drop_constraint(
        "ck_inbound_emails_sender_address_bidx_format", "inbound_emails", type_="check"
    )
    op.add_column("inbound_emails", sa.Column("sender_address_new", sa.String(320), nullable=True))
    _convert_inbound_emails(conn, encrypting=False)
    op.drop_column("inbound_emails", "sender_address")
    op.drop_column("inbound_emails", "sender_address_bidx")
    op.alter_column("inbound_emails", "sender_address_new", new_column_name="sender_address")

    op.add_column("model_calls", sa.Column("request_new", sa.Text(), nullable=True))
    op.add_column("model_calls", sa.Column("response_new", sa.Text(), nullable=True))
    _convert_model_calls(conn, encrypting=False)
    op.drop_column("model_calls", "request")
    op.drop_column("model_calls", "response")
    op.execute(
        "ALTER TABLE model_calls ALTER COLUMN request_new TYPE jsonb USING request_new::jsonb"
    )
    op.execute(
        "ALTER TABLE model_calls ALTER COLUMN response_new TYPE jsonb USING response_new::jsonb"
    )
    op.alter_column("model_calls", "request_new", new_column_name="request", nullable=False)
    op.alter_column("model_calls", "response_new", new_column_name="response")

    for table in ("model_calls", "inbound_emails"):
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
