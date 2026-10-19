// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
import { afterEach, describe, expect, it, vi } from "vitest";

import { DEFAULT_AUTH_SCHEMA, readServerEnv } from "../env";

import { AUTH_SCHEMA_SQL, AuthSchemaError, checkAuthSchema } from "./schema-check";

const base = {
  NODE_ENV: "production",
  BETTER_AUTH_URL: "https://web.example.test",
  BETTER_AUTH_SECRET: "x".repeat(40),
  PAIRLY_AUTH_DATABASE_URL: "postgresql://pairly_auth:pw@db.example.test:5432/pairly",
  PAIRLY_AUTH_ISSUER: "https://web.example.test",
  PAIRLY_AUTH_AUDIENCE: "https://web.example.test",
  PAIRLY_API_INTERNAL_URL: "https://api.example.test",
  PAIRLY_EMAIL_TRANSPORT: "smtp",
  PAIRLY_SMTP_HOST: "smtp.example.test",
};

describe("PAIRLY_AUTH_DB_SCHEMA", () => {
  afterEach(() => {
    vi.unstubAllEnvs();
  });

  function env(schema: string | undefined) {
    for (const [name, value] of Object.entries({ ...base, PAIRLY_AUTH_DB_SCHEMA: schema })) vi.stubEnv(name, value);
    return readServerEnv();
  }

  it("defaults to auth (local and compose unchanged)", () => {
    expect(DEFAULT_AUTH_SCHEMA).toBe("auth");
    expect(env(undefined).authSchema).toBe("auth");
  });

  it("takes pairly_auth for Supabase", () => {
    expect(env("pairly_auth").authSchema).toBe("pairly_auth");
  });

  it.each(["Auth", "pairly-auth", "a;drop", "1abc", "public", "pg_catalog", "extensions", "storage", "lg_checkpoints", ""])(
    "rejects %j",
    (schema) => {
      if (schema === "") {
        // Blank means unset: the default applies.
        expect(env(schema).authSchema).toBe("auth");
        return;
      }
      expect(() => env(schema)).toThrow(/PAIRLY_AUTH_DB_SCHEMA/);
    },
  );
});

describe("checkAuthSchema", () => {
  const query = (row: Record<string, unknown> | undefined) =>
    vi.fn(async (sql: string) => {
      expect(sql).toBe(AUTH_SCHEMA_SQL);
      return { rows: row ? [row] : [] };
    });

  it("passes when the search path resolves to the configured, owned schema", async () => {
    await expect(checkAuthSchema(query({ schema: "pairly_auth", owned: true }), "pairly_auth")).resolves.toBeUndefined();
  });

  it("stops a migration into Supabase's own auth schema", async () => {
    await expect(checkAuthSchema(query({ schema: "auth", owned: false }), "pairly_auth")).rejects.toThrow(AuthSchemaError);
  });

  it("stops when the search path resolves to no schema", async () => {
    await expect(checkAuthSchema(query({ schema: null, owned: null }), "auth")).rejects.toThrow(/no schema/);
  });

  it("stops when the schema is not owned by the role", async () => {
    await expect(checkAuthSchema(query({ schema: "auth", owned: false }), "auth")).rejects.toThrow(/not owned/);
  });
});
