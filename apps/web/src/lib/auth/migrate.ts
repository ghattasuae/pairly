// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology

/**
 * Better Auth schema migrations, run as `pairly_auth`, whose search path is the auth
 * schema (`PAIRLY_AUTH_DB_SCHEMA`: `auth` locally and in compose, `pairly_auth` on
 * Supabase, see infra/supabase/bootstrap.sql).
 *
 * Only additive changes run (new tables, columns and indexes). Better Auth refuses unsafe
 * changes by default (`UnsafeMigrationError`), and so do we: those need a reviewed manual
 * migration.
 *
 * Before anything runs, `checkAuthSchema` confirms the connection's current schema is the
 * configured one and is owned by the connected role. Better Auth creates its tables in
 * the current schema, so a wrong search path (for example Supabase's own `auth` schema,
 * or `public`) stops the migration instead of putting the tables there.
 */
import { getMigrations } from "better-auth/db/migration";
import { Pool } from "pg";

import { serverEnv } from "../env";

import { getAuth } from "./config";
import { checkAuthSchema } from "./schema-check";

async function withSchemaCheck(): Promise<void> {
  const env = serverEnv();
  const pool = new Pool({ connectionString: env.databaseUrl, max: 1, connectionTimeoutMillis: 5000 });
  try {
    await checkAuthSchema((sql) => pool.query(sql), env.authSchema);
  } finally {
    await pool.end();
  }
}

export async function pendingAuthMigrationSql(): Promise<string> {
  await withSchemaCheck();
  const { compileMigrations } = await getMigrations(getAuth().options);
  return compileMigrations();
}

export async function runAuthMigrations(log: (line: string) => void = console.log): Promise<void> {
  await withSchemaCheck();
  const { toBeCreated, toBeAdded, runMigrations } = await getMigrations(getAuth().options);
  if (toBeCreated.length === 0 && toBeAdded.length === 0) {
    log("auth schema: up to date");
    return;
  }
  const created = toBeCreated.map((t) => t.table).join(", ") || "none";
  const added = toBeAdded.map((t) => t.table).join(", ") || "none";
  log(`auth schema: creating tables [${created}], adding columns to [${added}]`);
  await runMigrations();
  log("auth schema: migrated");
}
