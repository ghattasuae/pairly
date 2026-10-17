// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology

/**
 * Per-address limit for magic link emails, on top of Better Auth's per-IP limit.
 *
 * At most `MAGIC_LINK_EMAIL_MAX` links per normalised address per
 * `MAGIC_LINK_EMAIL_WINDOW_SECONDS` (3 per 15 minutes), however many IPs ask. Over the
 * limit, no email is sent but the endpoint answers exactly as usual, so the response
 * never tells whether the address exists or was limited.
 *
 * Counts live in the `magicLinkEmailLimit` table (schema `auth`, created by the
 * `pairlyMagicLinkLimit` plugin's schema through the normal Better Auth migration). The
 * key is the SHA-256 of the address, so the table holds no email addresses. One atomic
 * upsert counts and decides, so concurrent requests cannot slip past the limit. The same
 * statement prunes up to `PRUNE_BATCH` expired rows of other addresses, so the table stays
 * small without a separate job.
 */
import { createHash } from "node:crypto";

import type { BetterAuthPlugin } from "better-auth";

export const MAGIC_LINK_EMAIL_MAX = 3;
export const MAGIC_LINK_EMAIL_WINDOW_SECONDS = 15 * 60;
export const LIMIT_TABLE = "magicLinkEmailLimit";

/** Adds the limit table to the Better Auth schema (migrated with the other tables). */
export const magicLinkLimitSchema = () =>
  ({
    id: "pairly-magic-link-limit",
    schema: {
      [LIMIT_TABLE]: {
        fields: {
          key: { type: "string", required: true, unique: true },
          count: { type: "number", required: true },
          windowStart: { type: "date", required: true },
        },
      },
    },
  }) satisfies BetterAuthPlugin;

export function normaliseEmail(email: string): string {
  return email.trim().toLowerCase();
}

export function limitKey(email: string): string {
  return createHash("sha256").update(`magic-link:${normaliseEmail(email)}`).digest("hex");
}

/** The subset of `pg.Pool` this module needs (easy to fake in tests). */
export interface Queryable {
  query: (sql: string, params: unknown[]) => Promise<{ rows: Array<Record<string, unknown>> }>;
}

/** Most expired rows one request removes, so pruning never makes a request slow. */
export const PRUNE_BATCH = 100;

// One statement: prune a bounded batch of expired rows (other addresses only, so the two
// parts never touch the same row), then count this request. A row whose window has
// passed is useless: the next request for it starts a new window anyway. A new window
// starts when the stored one is older than the window length. The id is only used for a
// first insert.
const UPSERT = `
WITH pruned AS (
  DELETE FROM "${LIMIT_TABLE}"
   WHERE "id" IN (
     SELECT "id" FROM "${LIMIT_TABLE}"
      WHERE "windowStart" <= now() - make_interval(secs => $3) AND "key" <> $2
      LIMIT ${PRUNE_BATCH}
      FOR UPDATE SKIP LOCKED)
)
INSERT INTO "${LIMIT_TABLE}" ("id", "key", "count", "windowStart")
VALUES ($1, $2, 1, now())
ON CONFLICT ("key") DO UPDATE SET
  "count" = CASE WHEN "${LIMIT_TABLE}"."windowStart" <= now() - make_interval(secs => $3)
                 THEN 1 ELSE "${LIMIT_TABLE}"."count" + 1 END,
  "windowStart" = CASE WHEN "${LIMIT_TABLE}"."windowStart" <= now() - make_interval(secs => $3)
                       THEN now() ELSE "${LIMIT_TABLE}"."windowStart" END
RETURNING "count"`;

/**
 * Count one request for `email` and say whether a link may be sent. Fails closed: if the
 * database cannot count, no link is sent.
 */
/** The SQL, exported for tests. */
export const QUOTA_SQL = UPSERT;

export async function takeMagicLinkQuota(db: Queryable, email: string): Promise<boolean> {
  try {
    const result = await db.query(UPSERT, [crypto.randomUUID(), limitKey(email), MAGIC_LINK_EMAIL_WINDOW_SECONDS]);
    const count = Number(result.rows[0]?.count);
    return Number.isFinite(count) && count <= MAGIC_LINK_EMAIL_MAX;
  } catch {
    return false;
  }
}
