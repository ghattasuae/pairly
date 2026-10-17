// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology

/**
 * Apply (or print) pending Better Auth schema migrations, as role pairly_auth.
 *
 *   pnpm --filter @pairly/web auth:migrate   # apply
 *   pnpm --filter @pairly/web auth:schema    # print the SQL only
 *
 * Reads the repo-root .env in development, like `pnpm dev`.
 */
import path from "node:path";
import { fileURLToPath } from "node:url";

import { pendingAuthMigrationSql, runAuthMigrations } from "../src/lib/auth/migrate";

async function main(): Promise<void> {
  if (process.env.NODE_ENV !== "production") {
    (process.env as Record<string, string | undefined>).NODE_ENV ??= "development";
    try {
      process.loadEnvFile(path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../../.env"));
    } catch {
      // No repo-root .env: rely on the environment.
    }
  }
  if (process.argv.includes("--print")) {
    const sql = await pendingAuthMigrationSql();
    console.log(sql.trim() ? sql : "-- auth schema is up to date");
  } else {
    await runAuthMigrations();
  }
  process.exit(0);
}

main().catch((error: unknown) => {
  console.error(error);
  process.exit(1);
});
