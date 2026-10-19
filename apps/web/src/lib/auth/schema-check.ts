// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology

/**
 * The guard `migrate.ts` runs before any Better Auth migration: the connection's current
 * schema must be the configured auth schema (`PAIRLY_AUTH_DB_SCHEMA`) and owned by the
 * connected role. No imports, so it is unit tested without a database.
 */

type Query = (sql: string) => Promise<{ rows: Array<Record<string, unknown>> }>;

export class AuthSchemaError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "AuthSchemaError";
  }
}

export const AUTH_SCHEMA_SQL = `SELECT current_schema() AS schema,
  (SELECT n.nspowner = r.oid FROM pg_namespace n, pg_roles r
    WHERE n.nspname = current_schema() AND r.rolname = current_user) AS owned`;

/** Throw unless the current schema is `expected` and the connected role owns it. */
export async function checkAuthSchema(query: Query, expected: string): Promise<void> {
  const { rows } = await query(AUTH_SCHEMA_SQL);
  const row = rows[0] ?? {};
  const current = typeof row.schema === "string" ? row.schema : null;
  if (current !== expected) {
    throw new AuthSchemaError(
      `auth schema: the search path resolves to ${current === null ? "no schema" : JSON.stringify(current)}, ` +
        `expected ${JSON.stringify(expected)} (PAIRLY_AUTH_DB_SCHEMA). Set the role's search_path ` +
        `(ALTER ROLE pairly_auth SET search_path = ${expected}) before migrating.`,
    );
  }
  if (row.owned !== true) {
    throw new AuthSchemaError(`auth schema: ${JSON.stringify(expected)} is not owned by the connected role`);
  }
}
