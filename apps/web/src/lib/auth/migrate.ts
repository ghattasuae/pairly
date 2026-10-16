// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology

/**
 * Better Auth schema migrations, run as `pairly_auth` (search path: schema `auth`).
 *
 * Only additive changes run (new tables, columns and indexes). Better Auth refuses unsafe
 * changes by default (`UnsafeMigrationError`), and so do we: those need a reviewed manual
 * migration.
 */
import { getMigrations } from "better-auth/db/migration";

import { getAuth } from "./config";

export async function pendingAuthMigrationSql(): Promise<string> {
  const { compileMigrations } = await getMigrations(getAuth().options);
  return compileMigrations();
}

export async function runAuthMigrations(log: (line: string) => void = console.log): Promise<void> {
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
