// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology

/**
 * Runs once when the server starts, before it takes requests. With
 * PAIRLY_AUTH_MIGRATE_ON_START=true (compose sets it) it applies pending Better Auth schema
 * migrations as pairly_auth. A failure stops the server: better no web app than one whose
 * auth tables do not match its code.
 */
export async function register(): Promise<void> {
  if (process.env.NEXT_RUNTIME !== "nodejs") return;
  const { serverEnv } = await import("./lib/env");
  const env = serverEnv();
  if (!env.migrateOnStart) return;
  const { runAuthMigrations } = await import("./lib/auth/migrate");
  await runAuthMigrations((line) => console.log(`[pairly-web] ${line}`));
}
