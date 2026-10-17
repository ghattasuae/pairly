// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology

/**
 * Hosted demo sign-in: the rules, with no I/O (the route handler wires them up).
 *
 * A public demo has no mailbox a visitor can read, so `/<locale>/demo` lists the seeded
 * demo users and signs the visitor in as the one they pick: the server creates a normal
 * magic link for that address (Better Auth, single use, 5 minutes) and redirects the
 * browser to it instead of emailing it. No email is sent and no mailbox is shown.
 *
 * Locks, all needed (fail closed):
 *
 * 1. `PAIRLY_DEMO_PUBLIC_SIGNIN=true` on the web server, and `PAIRLY_ENV` set to
 *    something other than `production` (unset counts as production).
 * 2. The API offers the address: `GET /v1/demo/signin-accounts` answers only when the
 *    API has the same switch on and the demo organisation turned its
 *    `demo.public_signin` flag on, and it lists only the seeded demo users (never the
 *    admin). The address must be on that answer, checked at the moment of the request.
 * 3. Same origin: the form post must come from this site (`Origin` is one of the
 *    trusted origins), so another site cannot sign a visitor in behind their back.
 * 4. A per-address limit (`DEMO_SIGNIN_MAX` per `DEMO_SIGNIN_WINDOW_MS`) on this server.
 */
import { z } from "zod";

export const DEMO_SIGNIN_MAX = 10;
export const DEMO_SIGNIN_WINDOW_MS = 10 * 60 * 1000;
const MAX_KEYS = 10_000;

export function demoSigninEnabled(env: Record<string, string | undefined>): boolean {
  const raw = (env.PAIRLY_DEMO_PUBLIC_SIGNIN ?? "").trim().toLowerCase();
  const on = ["1", "true", "yes", "on"].includes(raw);
  const pairlyEnv = (env.PAIRLY_ENV ?? "").trim().toLowerCase() || "production";
  return on && pairlyEnv !== "production";
}

const accountSchema = z.object({
  email: z
    .string()
    .max(320)
    .regex(/^[a-z0-9][a-z0-9.+-]*@example\.test$/, "demo accounts use @example.test"),
  label: z.string().regex(/^[a-z_]{1,40}$/),
  role: z.string().max(64),
  workflow: z.string().max(32).nullable(),
});

const answerSchema = z.object({
  org_slug: z.string().max(63),
  accounts: z.array(accountSchema).max(50),
});

export type DemoAccount = z.infer<typeof accountSchema>;

/** The API's answer, validated. Anything unexpected means: no accounts. */
export function parseAccounts(data: unknown): DemoAccount[] {
  const parsed = answerSchema.safeParse(data);
  return parsed.success ? parsed.data.accounts : [];
}

/** The `Origin` of a form post is one of ours (exact match). A missing origin fails. */
export function sameOrigin(origin: string | null, trusted: readonly string[]): boolean {
  return origin !== null && trusted.includes(origin);
}

/** At most `limit` hits per key in any `windowMs`, in this process. Bounded memory. */
export class DemoSigninLimiter {
  private readonly hits = new Map<string, number[]>();

  constructor(
    private readonly limit = DEMO_SIGNIN_MAX,
    private readonly windowMs = DEMO_SIGNIN_WINDOW_MS,
    private readonly now: () => number = Date.now,
  ) {}

  take(key: string): boolean {
    const at = this.now();
    const recent = (this.hits.get(key) ?? []).filter((t) => at - t < this.windowMs);
    this.hits.delete(key);
    if (recent.length >= this.limit) {
      this.hits.set(key, recent);
      return false;
    }
    recent.push(at);
    this.hits.set(key, recent);
    while (this.hits.size > MAX_KEYS) {
      const oldest = this.hits.keys().next().value;
      if (oldest === undefined) break;
      this.hits.delete(oldest);
    }
    return true;
  }
}

export type DemoSigninOutcome =
  | { kind: "not_found" }
  | { kind: "forbidden" }
  | { kind: "error"; code: "rate_limited" | "unknown_account" | "unavailable" }
  | { kind: "redirect"; location: string };

export interface DemoSigninRequest {
  enabled: boolean;
  origin: string | null;
  trustedOrigins: readonly string[];
  clientKey: string;
  email: string;
  limiter: DemoSigninLimiter;
  /** The accounts the API offers right now (`[]` when it says no or fails). */
  fetchAccounts: () => Promise<DemoAccount[]>;
  /** A magic link for `email`, captured instead of emailed, or null. */
  issueLink: (email: string) => Promise<string | null>;
}

export async function demoSignin(req: DemoSigninRequest): Promise<DemoSigninOutcome> {
  if (!req.enabled) return { kind: "not_found" };
  if (!sameOrigin(req.origin, req.trustedOrigins)) return { kind: "forbidden" };
  if (!req.limiter.take(req.clientKey)) return { kind: "error", code: "rate_limited" };
  const email = req.email.trim().toLowerCase();
  const accounts = await req.fetchAccounts();
  // The API says no (switch or flag off, no demo organisation): as if the page did not exist.
  if (accounts.length === 0) return { kind: "not_found" };
  if (!accounts.some((a) => a.email === email)) return { kind: "error", code: "unknown_account" };
  const link = await req.issueLink(email);
  if (!link) return { kind: "error", code: "unavailable" };
  return { kind: "redirect", location: link };
}
