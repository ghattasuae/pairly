// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
import { afterEach, describe, expect, it, vi } from "vitest";

import { readServerEnv } from "../env";

import { MAGIC_LINK_IP_MAX, magicLinkIpRule } from "./config";

// PAIRLY_AUTH_MAGIC_LINK_RATE_LIMIT: the e2e suite's override of the per-IP magic link
// limit. Honoured in development and test, refused (fail closed) everywhere else.
describe("magic link rate limit override", () => {
  const base = {
    NODE_ENV: "production",
    BETTER_AUTH_URL: "https://web.example.test",
    BETTER_AUTH_SECRET: "x".repeat(40),
    PAIRLY_AUTH_DATABASE_URL: "postgresql://pairly_auth:pw@db.example.test:5432/pairly",
    PAIRLY_AUTH_ISSUER: "https://web.example.test",
    PAIRLY_AUTH_AUDIENCE: "https://web.example.test",
    PAIRLY_API_INTERNAL_URL: "https://api.example.test",
    PAIRLY_SMTP_HOST: "smtp.example.test",
    PAIRLY_ENV: "production",
  };

  function withEnv(extra: Record<string, string | undefined>) {
    for (const [name, value] of Object.entries({ ...base, ...extra })) vi.stubEnv(name, value);
  }

  afterEach(() => {
    vi.unstubAllEnvs();
  });

  it("keeps 5 per 5 minutes per IP when unset", () => {
    withEnv({ PAIRLY_AUTH_MAGIC_LINK_RATE_LIMIT: undefined });
    const env = readServerEnv();
    expect(env.magicLinkRateLimit).toBeUndefined();
    expect(magicLinkIpRule(env)).toEqual({ window: 300, max: MAGIC_LINK_IP_MAX });
    expect(MAGIC_LINK_IP_MAX).toBe(5);
  });

  it("refuses to start with the override in production", () => {
    withEnv({ PAIRLY_AUTH_MAGIC_LINK_RATE_LIMIT: "200" });
    expect(() => readServerEnv()).toThrow(/PAIRLY_AUTH_MAGIC_LINK_RATE_LIMIT is for development and test only/);
  });

  it("refuses it when PAIRLY_ENV is missing or anything but development and test", () => {
    withEnv({ PAIRLY_AUTH_MAGIC_LINK_RATE_LIMIT: "200", PAIRLY_ENV: undefined });
    expect(() => readServerEnv()).toThrow(/development and test only/);
    withEnv({ PAIRLY_AUTH_MAGIC_LINK_RATE_LIMIT: "200", PAIRLY_ENV: "staging" });
    expect(() => readServerEnv()).toThrow(/development and test only/);
  });

  it.each(["development", "test"])("honours it when PAIRLY_ENV is %s", (pairlyEnv) => {
    withEnv({ PAIRLY_AUTH_MAGIC_LINK_RATE_LIMIT: "200", PAIRLY_ENV: pairlyEnv });
    const env = readServerEnv();
    expect(env.magicLinkRateLimit).toBe(200);
    expect(magicLinkIpRule(env)).toEqual({ window: 300, max: 200 });
  });

  it.each(["0", "-1", "1.5", "lots", "100000"])("rejects %s as a limit", (raw) => {
    withEnv({ PAIRLY_AUTH_MAGIC_LINK_RATE_LIMIT: raw, PAIRLY_ENV: "development" });
    expect(() => readServerEnv()).toThrow(/magicLinkRateLimit/);
  });
});
