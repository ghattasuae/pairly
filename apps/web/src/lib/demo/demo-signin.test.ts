// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
import { describe, expect, it, vi } from "vitest";

import { isBlockedAuthPath, isDemoBlockedAuthPath } from "../auth/blocked-paths";
import { captureFor, captureMagicLink } from "../auth/demo-capture";
import { isPublicPath } from "../public-paths";

import {
  type DemoAccount,
  DemoSigninLimiter,
  demoSignin,
  demoSigninEnabled,
  parseAccounts,
  sameOrigin,
} from "./policy";

const SITE = "https://pairly-demo-web.example";
const RECRUITER: DemoAccount = {
  email: "demo-hire-recruiter@example.test",
  label: "recruiter",
  role: "recruiter",
  workflow: "hire",
};

function request(overrides: Partial<Parameters<typeof demoSignin>[0]> = {}) {
  return {
    enabled: true,
    origin: SITE,
    trustedOrigins: [SITE],
    clientKey: "203.0.113.7",
    email: RECRUITER.email,
    limiter: new DemoSigninLimiter(),
    fetchAccounts: vi.fn().mockResolvedValue([RECRUITER]),
    issueLink: vi.fn().mockResolvedValue(`${SITE}/en/sign-in/confirm#token=abc`),
    ...overrides,
  };
}

describe("demoSigninEnabled", () => {
  it("is off by default, and never in production", () => {
    expect(demoSigninEnabled({})).toBe(false);
    expect(demoSigninEnabled({ PAIRLY_ENV: "staging" })).toBe(false);
    expect(demoSigninEnabled({ PAIRLY_DEMO_PUBLIC_SIGNIN: "true" })).toBe(false);
    expect(demoSigninEnabled({ PAIRLY_DEMO_PUBLIC_SIGNIN: "true", PAIRLY_ENV: "production" })).toBe(false);
    expect(demoSigninEnabled({ PAIRLY_DEMO_PUBLIC_SIGNIN: "false", PAIRLY_ENV: "staging" })).toBe(false);
    expect(demoSigninEnabled({ PAIRLY_DEMO_PUBLIC_SIGNIN: "true", PAIRLY_ENV: "staging" })).toBe(true);
  });
});

describe("parseAccounts", () => {
  it("keeps demo addresses only and fails closed on anything else", () => {
    expect(parseAccounts({ org_slug: "demo", accounts: [RECRUITER] })).toEqual([RECRUITER]);
    expect(parseAccounts({ org_slug: "demo", accounts: [{ ...RECRUITER, email: "ceo@acme.com" }] })).toEqual([]);
    expect(parseAccounts({ accounts: "nope" })).toEqual([]);
    expect(parseAccounts(null)).toEqual([]);
  });
});

describe("sameOrigin", () => {
  it("needs an exact trusted origin", () => {
    expect(sameOrigin(SITE, [SITE])).toBe(true);
    expect(sameOrigin("https://evil.example", [SITE])).toBe(false);
    expect(sameOrigin(null, [SITE])).toBe(false);
  });
});

describe("demoSignin", () => {
  it("redirects to a fresh magic link for an offered demo user", async () => {
    const req = request({ email: " Demo-Hire-Recruiter@example.test " });
    expect(await demoSignin(req)).toEqual({ kind: "redirect", location: `${SITE}/en/sign-in/confirm#token=abc` });
    expect(req.issueLink).toHaveBeenCalledWith(RECRUITER.email);
  });

  it("does not exist when the switch is off, and never asks the API or Better Auth", async () => {
    const req = request({ enabled: false });
    expect(await demoSignin(req)).toEqual({ kind: "not_found" });
    expect(req.fetchAccounts).not.toHaveBeenCalled();
    expect(req.issueLink).not.toHaveBeenCalled();
  });

  it("does not exist when the API offers nobody (flag off, no demo organisation)", async () => {
    const req = request({ fetchAccounts: vi.fn().mockResolvedValue([]) });
    expect(await demoSignin(req)).toEqual({ kind: "not_found" });
    expect(req.issueLink).not.toHaveBeenCalled();
  });

  it("refuses a post from another site", async () => {
    for (const origin of ["https://evil.example", null]) {
      const req = request({ origin });
      expect(await demoSignin(req)).toEqual({ kind: "forbidden" });
      expect(req.issueLink).not.toHaveBeenCalled();
    }
  });

  it("never issues a link for an address the API did not offer", async () => {
    for (const email of ["demo-admin@example.test", "someone@acme.com", ""]) {
      const req = request({ email });
      expect(await demoSignin(req)).toEqual({ kind: "error", code: "unknown_account" });
      expect(req.issueLink).not.toHaveBeenCalled();
    }
  });

  it("limits sign-ins per client address", async () => {
    const limiter = new DemoSigninLimiter(2, 60_000, () => 1_000);
    expect((await demoSignin(request({ limiter }))).kind).toBe("redirect");
    expect((await demoSignin(request({ limiter }))).kind).toBe("redirect");
    expect(await demoSignin(request({ limiter }))).toEqual({ kind: "error", code: "rate_limited" });
    expect((await demoSignin(request({ limiter, clientKey: "198.51.100.1" }))).kind).toBe("redirect");
  });

  it("says unavailable when no link could be made", async () => {
    const req = request({ issueLink: vi.fn().mockResolvedValue(null) });
    expect(await demoSignin(req)).toEqual({ kind: "error", code: "unavailable" });
  });
});

describe("DemoSigninLimiter", () => {
  it("opens again after the window", () => {
    let now = 0;
    const limiter = new DemoSigninLimiter(1, 1_000, () => now);
    expect(limiter.take("a")).toBe(true);
    expect(limiter.take("a")).toBe(false);
    now = 1_000;
    expect(limiter.take("a")).toBe(true);
  });
});

describe("captureMagicLink", () => {
  it("captures only for the address of this call chain", async () => {
    const link = await captureMagicLink(RECRUITER.email, async () => {
      expect(captureFor("someone@acme.com")).toBeNull();
      captureFor(RECRUITER.email.toUpperCase())?.keep("https://x.example/#token=1");
    });
    expect(link).toBe("https://x.example/#token=1");
    // Outside a capture nothing is captured: the normal email path runs.
    expect(captureFor(RECRUITER.email)).toBeNull();
  });

  it("does not leak into a concurrent call", async () => {
    let other: ReturnType<typeof captureFor> = null;
    const outside = new Promise<void>((resolve) => {
      setTimeout(() => {
        other = captureFor(RECRUITER.email);
        resolve();
      }, 0);
    });
    await captureMagicLink(RECRUITER.email, async () => {
      await new Promise((r) => setTimeout(r, 5));
    });
    await outside;
    expect(other).toBeNull();
  });
});

describe("auth paths on the hosted demo", () => {
  it("closes the endpoints that would change a shared account, only in the demo", () => {
    for (const path of [
      "/api/auth/two-factor/enable",
      "/api/auth/two-factor/disable",
      "/api/auth/change-email",
      "/api/auth/update-user",
      "/api/auth/delete-user",
      "/api/auth/revoke-other-sessions",
    ]) {
      expect(isDemoBlockedAuthPath(path, true)).toBe(true);
      expect(isDemoBlockedAuthPath(path, false)).toBe(false);
    }
    expect(isDemoBlockedAuthPath("/api/auth/magic-link/verify", true)).toBe(false);
    expect(isDemoBlockedAuthPath("/api/auth/two-factor/verify-totp", true)).toBe(false);
    expect(isBlockedAuthPath("/api/auth/token")).toBe(true);
  });

  it("serves the demo page without a session (the page itself 404s when off)", () => {
    expect(isPublicPath("/demo")).toBe(true);
    expect(isPublicPath("/demox")).toBe(false);
  });
});

describe("the fake email transport (hosted demo)", () => {
  const base = {
    NODE_ENV: "production",
    BETTER_AUTH_URL: SITE,
    BETTER_AUTH_SECRET: "x".repeat(40),
    PAIRLY_AUTH_DATABASE_URL: "postgresql://pairly_auth:pw@db.internal:5432/pairly",
    PAIRLY_AUTH_ISSUER: SITE,
    PAIRLY_AUTH_AUDIENCE: SITE,
    PAIRLY_API_INTERNAL_URL: "http://api.internal:8000",
    PAIRLY_EMAIL_TRANSPORT: "fake",
  };

  async function read(env: Record<string, string>) {
    for (const [k, v] of Object.entries({ ...base, ...env })) vi.stubEnv(k, v);
    const { readServerEnv } = await import("../env");
    try {
      return readServerEnv();
    } finally {
      vi.unstubAllEnvs();
    }
  }

  it("needs no SMTP host outside production", async () => {
    const env = await read({ PAIRLY_ENV: "staging" });
    expect(env.email.transport).toBe("fake");
  });

  it("is refused in production", async () => {
    await expect(read({ PAIRLY_ENV: "production" })).rejects.toThrow(/fake is not allowed in production/);
  });
});
