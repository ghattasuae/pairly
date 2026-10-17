// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology

/**
 * Server settings for the web app, read from the environment on first use.
 *
 * Nothing here runs at import time, so `next build` works without secrets. The first
 * request (or the migration script) validates everything and fails closed: a missing
 * secret stops the server instead of falling back to a default.
 *
 * In development only, a few values are derived from the repo-root `.env` (the one
 * `make dev` creates) so `pnpm --filter web dev` works against the compose stack with no
 * extra setup. Compose and production always pass every value explicitly.
 */
import { z } from "zod";

const url = z.url({ protocol: /^https?$/ });

const schema = z.object({
  nodeEnv: z.enum(["development", "production", "test"]),
  baseUrl: url,
  /** Exact origins Better Auth accepts requests and redirects from (BETTER_AUTH_URL first). */
  trustedOrigins: z.array(z.string()).min(1),
  secret: z.string().min(32, "BETTER_AUTH_SECRET must be at least 32 characters"),
  databaseUrl: z.string().regex(/^postgres(ql)?:\/\//, "PAIRLY_AUTH_DATABASE_URL must be a postgres URL"),
  jwtIssuer: z.string().min(1),
  jwtAudience: z.string().min(1),
  apiInternalUrl: url,
  /**
   * `smtp` (any relay; Mailpit for the Docker stack), `outbox` (native development and
   * tests only: each message is written as files to `outboxDir`, see `outbox.ts`) or
   * `fake` (sends nothing; the hosted demo, whose visitors sign in through the demo page
   * and must never make it email anyone; refused when `PAIRLY_ENV` is production, as
   * the API's `fake` transport is).
   */
  email: z.object({
    transport: z.enum(["smtp", "outbox", "fake"]),
    outboxDir: z.string().optional(),
  }),
  smtp: z.object({
    host: z.string().optional(),
    port: z.coerce.number().int().min(1).max(65535),
    secure: z.boolean(),
    requireTls: z.boolean(),
    user: z.string().optional(),
    password: z.string().optional(),
    from: z.string().min(3),
  }),
  migrateOnStart: z.boolean(),
  google: z.object({ clientId: z.string(), clientSecret: z.string() }).optional(),
  microsoft: z
    .object({ clientId: z.string(), clientSecret: z.string(), tenantId: z.string() })
    .optional(),
});

export type ServerEnv = z.infer<typeof schema>;

function value(name: string): string | undefined {
  const raw = process.env[name];
  return raw === undefined || raw.trim() === "" ? undefined : raw.trim();
}

function flag(name: string, fallback: boolean): boolean {
  const raw = value(name);
  if (raw === undefined) return fallback;
  return ["1", "true", "yes", "on"].includes(raw.toLowerCase());
}

/**
 * Parse `PAIRLY_AUTH_TRUSTED_ORIGINS`: a comma separated list of exact origins
 * (`scheme://host[:port]`). No wildcards, paths, queries or credentials: anything else is a
 * configuration error, so a typo can never widen what Better Auth trusts.
 */
export function parseTrustedOrigins(raw: string | undefined): string[] {
  if (!raw) return [];
  const origins: string[] = [];
  for (const entry of raw.split(",")) {
    const candidate = entry.trim();
    if (!candidate) continue;
    let parsed: URL;
    try {
      parsed = new URL(candidate);
    } catch {
      throw new Error(`PAIRLY_AUTH_TRUSTED_ORIGINS: ${JSON.stringify(candidate)} is not a URL`);
    }
    // Equal to its own origin means: no path, query, fragment or credentials.
    const exact =
      (parsed.protocol === "http:" || parsed.protocol === "https:") &&
      !candidate.includes("*") &&
      candidate.replace(/\/$/, "") === parsed.origin;
    if (!exact) {
      throw new Error(
        `PAIRLY_AUTH_TRUSTED_ORIGINS: ${JSON.stringify(candidate)} must be an exact origin such as http://127.0.0.1:3100`,
      );
    }
    if (!origins.includes(parsed.origin)) origins.push(parsed.origin);
  }
  return origins;
}

/** Development-only fallbacks, derived from the repo-root `.env`. */
function devDefaults(): Partial<Record<string, string>> {
  const pw = value("PAIRLY_AUTH_DB_PASSWORD");
  const db = value("POSTGRES_DB") ?? "pairly";
  const pgPort = value("POSTGRES_HOST_PORT") ?? "5432";
  const web = value("PAIRLY_PUBLIC_WEB_URL") ?? "http://localhost:3100";
  return {
    BETTER_AUTH_URL: web,
    PAIRLY_AUTH_DATABASE_URL: pw
      ? `postgresql://pairly_auth:${encodeURIComponent(pw)}@localhost:${pgPort}/${db}`
      : undefined,
    PAIRLY_AUTH_ISSUER: web,
    PAIRLY_AUTH_AUDIENCE: web,
    PAIRLY_API_INTERNAL_URL: value("PAIRLY_PUBLIC_API_URL") ?? "http://localhost:8000",
    PAIRLY_SMTP_HOST: "localhost",
    PAIRLY_SMTP_PORT: value("MAILPIT_SMTP_HOST_PORT") ?? "1025",
  };
}

export function readServerEnv(): ServerEnv {
  const nodeEnv = (process.env.NODE_ENV ?? "production") as ServerEnv["nodeEnv"];
  const dev = nodeEnv === "development";
  const fallback = dev ? devDefaults() : {};
  const get = (name: string): string | undefined => value(name) ?? fallback[name];

  const googleId = get("GOOGLE_CLIENT_ID");
  const googleSecret = get("GOOGLE_CLIENT_SECRET");
  const msId = get("MICROSOFT_CLIENT_ID");
  const msSecret = get("MICROSOFT_CLIENT_SECRET");

  const baseUrl = get("BETTER_AUTH_URL");
  let baseOrigin: string[] = [];
  try {
    baseOrigin = baseUrl ? [new URL(baseUrl).origin] : [];
  } catch {
    // Reported by the schema below.
  }
  const extraOrigins = parseTrustedOrigins(get("PAIRLY_AUTH_TRUSTED_ORIGINS"));

  const emailTransport = get("PAIRLY_EMAIL_TRANSPORT") ?? "smtp";
  const parsed = schema.safeParse({
    nodeEnv,
    baseUrl,
    trustedOrigins: [...new Set([...baseOrigin, ...extraOrigins])],
    secret: get("BETTER_AUTH_SECRET"),
    databaseUrl: get("PAIRLY_AUTH_DATABASE_URL"),
    jwtIssuer: get("PAIRLY_AUTH_ISSUER"),
    jwtAudience: get("PAIRLY_AUTH_AUDIENCE"),
    apiInternalUrl: get("PAIRLY_API_INTERNAL_URL"),
    email: { transport: emailTransport, outboxDir: get("PAIRLY_OUTBOX_DIR") },
    smtp: {
      host: get("PAIRLY_SMTP_HOST"),
      port: get("PAIRLY_SMTP_PORT") ?? "587",
      secure: flag("PAIRLY_SMTP_SECURE", false),
      // STARTTLS required. Turn on for any real relay; Mailpit locally has no TLS.
      requireTls: flag("PAIRLY_SMTP_REQUIRE_TLS", false),
      user: get("PAIRLY_SMTP_USER"),
      password: get("PAIRLY_SMTP_PASSWORD"),
      from: get("PAIRLY_SMTP_FROM") ?? "Pairly <no-reply@pairly.localhost>",
    },
    migrateOnStart: flag("PAIRLY_AUTH_MIGRATE_ON_START", false),
    // SSO stays off unless both halves of the credential exist.
    google:
      googleId && googleSecret ? { clientId: googleId, clientSecret: googleSecret } : undefined,
    microsoft:
      msId && msSecret
        ? { clientId: msId, clientSecret: msSecret, tenantId: get("MICROSOFT_TENANT_ID") ?? "common" }
        : undefined,
  });
  if (!parsed.success) {
    const fields = parsed.error.issues.map((i) => `${i.path.join(".")}: ${i.message}`);
    throw new Error(`Invalid web server configuration:\n  ${fields.join("\n  ")}`);
  }
  const env = parsed.data;
  if (env.email.transport === "smtp" && !env.smtp.host) {
    throw new Error("Invalid web server configuration:\n  smtp.host: PAIRLY_SMTP_HOST is required");
  }
  if (env.email.transport === "outbox") {
    // Fail closed: the outbox only exists for the native development stack and tests.
    // PAIRLY_ENV, not NODE_ENV, decides (the native stack may run `next start`).
    const pairlyEnv = get("PAIRLY_ENV") ?? "production";
    if (pairlyEnv !== "development" && pairlyEnv !== "test") {
      throw new Error("PAIRLY_EMAIL_TRANSPORT=outbox is for development and test only (PAIRLY_ENV)");
    }
    if (!env.email.outboxDir) {
      throw new Error("Invalid web server configuration:\n  email.outboxDir: PAIRLY_OUTBOX_DIR is required");
    }
  }
  if (env.email.transport === "fake") {
    const pairlyEnv = get("PAIRLY_ENV") ?? "production";
    if (pairlyEnv === "production") {
      throw new Error("PAIRLY_EMAIL_TRANSPORT=fake is not allowed in production (PAIRLY_ENV)");
    }
  }
  if (env.nodeEnv === "production" && !env.baseUrl.startsWith("https://")) {
    const host = new URL(env.baseUrl).hostname;
    if (host !== "localhost" && host !== "127.0.0.1") {
      throw new Error("BETTER_AUTH_URL must use https outside localhost");
    }
  }
  return env;
}

let cached: ServerEnv | undefined;

export function serverEnv(): ServerEnv {
  cached ??= readServerEnv();
  return cached;
}

/** Which SSO buttons to show. Safe to pass to the client: booleans only. */
export function ssoAvailability(): { google: boolean; microsoft: boolean } {
  const env = serverEnv();
  return { google: env.google !== undefined, microsoft: env.microsoft !== undefined };
}
