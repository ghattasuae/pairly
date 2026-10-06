// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
import { describe, expect, it, vi } from "vitest";

import { amrClaim, amrForPath } from "./auth/amr";
import { isBlockedAuthPath } from "./auth/blocked-paths";
import { callbackFromVerifyUrl, confirmUrl } from "./auth/config";
import {
  limitKey,
  MAGIC_LINK_EMAIL_MAX,
  MAGIC_LINK_EMAIL_WINDOW_SECONDS,
  type Queryable,
  takeMagicLinkQuota,
} from "./auth/magic-link-limit";
import { nextFromCallback } from "./auth/two-factor-gate";
import { isWellFormedInviteToken } from "./invite-token";
import { isPublicPath } from "./public-paths";
import { safeNextPath } from "./safe-path";

const TOKEN = "AbCdEfGhIjKlMnOpQrStUvWxYz0123456789-_abcde";

describe("amr", () => {
  it("marks only a TOTP verification as otp", () => {
    expect(amrForPath("/two-factor/verify-totp")).toBe("otp");
    expect(amrForPath("/magic-link/verify")).toBe("email");
    expect(amrForPath("/callback/:id")).toBe("sso");
    expect(amrForPath("/oauth2/callback/:id")).toBe("sso");
  });

  it("fails closed for backup codes, 2FA changes and unknown paths", () => {
    expect(amrForPath("/two-factor/verify-backup-code")).toBe("none");
    expect(amrForPath("/two-factor/disable")).toBe("none");
    expect(amrForPath("/two-factor/enable")).toBe("none");
    expect(amrForPath("/constructor")).toBe("none");
    expect(amrForPath(undefined)).toBe("none");
  });

  it("puts exactly one known method in the claim", () => {
    expect(amrClaim("otp")).toEqual(["otp"]);
    expect(amrClaim("email")).toEqual(["email"]);
    expect(amrClaim(undefined)).toEqual(["none"]);
    expect(amrClaim("OTP")).toEqual(["none"]);
    expect(amrClaim(["otp"])).toEqual(["none"]);
  });
});

describe("isBlockedAuthPath", () => {
  it("closes token minting and the organization endpoints to browsers", () => {
    expect(isBlockedAuthPath("/api/auth/token")).toBe(true);
    expect(isBlockedAuthPath("/api/auth/token/")).toBe(true);
    expect(isBlockedAuthPath("/api/auth/TOKEN")).toBe(true);
    expect(isBlockedAuthPath("/api/auth/organization/set-active")).toBe(true);
    expect(isBlockedAuthPath("/api/auth/organization/invite-member")).toBe(true);
    expect(isBlockedAuthPath("/api/auth/organization/accept-invitation")).toBe(true);
  });

  it("keeps sign-in, 2FA and the key set open", () => {
    expect(isBlockedAuthPath("/api/auth/jwks")).toBe(false);
    expect(isBlockedAuthPath("/api/auth/sign-in/magic-link")).toBe(false);
    expect(isBlockedAuthPath("/api/auth/magic-link/verify")).toBe(false);
    expect(isBlockedAuthPath("/api/auth/two-factor/verify-totp")).toBe(false);
    expect(isBlockedAuthPath("/api/auth/get-session")).toBe(false);
    expect(isBlockedAuthPath("/api/auth/tokens")).toBe(false);
  });
});

describe("the auth route handler", () => {
  it("answers 404 for /token and /organization/* before Better Auth runs", async () => {
    const getAuth = vi.fn();
    vi.doMock("@/lib/auth/config", () => ({ getAuth }));
    const route = await import("@/app/api/auth/[...all]/route");
    const token = await route.GET(new Request("http://localhost:3000/api/auth/token"));
    expect(token.status).toBe(404);
    const setActive = await route.POST(
      new Request("http://localhost:3000/api/auth/organization/set-active", { method: "POST", body: "{}" }),
    );
    expect(setActive.status).toBe(404);
    expect(getAuth).not.toHaveBeenCalled();
    vi.doUnmock("@/lib/auth/config");
  });
});

describe("safeNextPath", () => {
  it("allows same-site locale pages only", () => {
    expect(safeNextPath("/en")).toBe("/en");
    expect(safeNextPath("/ar/")).toBe("/ar");
    expect(safeNextPath(`/en/invite/${TOKEN}`)).toBe(`/en/invite/${TOKEN}`);
    expect(safeNextPath("/ar/settings/members")).toBe("/ar/settings/members");
  });

  it.each([
    "//evil.example",
    "/\\evil.example",
    "https://evil.example/en",
    "/fr/inbox",
    "/en/../admin",
    "/en/a?x=1",
    "/en/a#x",
    "/en//a",
    "en/inbox",
    "",
    "/en/%2e%2e",
  ])("refuses %s", (value) => {
    expect(safeNextPath(value)).toBeNull();
  });

  it("refuses non-strings", () => {
    expect(safeNextPath(undefined)).toBeNull();
    expect(safeNextPath(["/en"])).toBeNull();
  });
});

describe("magic link continuation", () => {
  it("carries a safe next page in the fragment, never the query", () => {
    const url = new URL(confirmUrl("http://localhost:3000", "en", "tok123", `/en/invite/${TOKEN}`));
    expect(url.search).toBe("");
    const fragment = new URLSearchParams(url.hash.slice(1));
    expect(fragment.get("token")).toBe("tok123");
    expect(fragment.get("next")).toBe(`/en/invite/${TOKEN}`);
  });

  it("drops an unsafe next page", () => {
    const url = new URL(confirmUrl("http://localhost:3000", "en", "tok123", "//evil.example"));
    expect(new URLSearchParams(url.hash.slice(1)).get("next")).toBeNull();
  });

  it("reads the callback from Better Auth's verify URL", () => {
    const verify = `http://localhost:3000/api/auth/magic-link/verify?token=x&callbackURL=${encodeURIComponent(`/ar/invite/${TOKEN}`)}`;
    expect(callbackFromVerifyUrl(verify)).toBe(`/ar/invite/${TOKEN}`);
    expect(callbackFromVerifyUrl("http://localhost:3000/x?callbackURL=https://evil.example")).toBeNull();
    expect(callbackFromVerifyUrl("not a url")).toBeNull();
  });

  it("keeps the invitation page across the two-factor step", () => {
    expect(nextFromCallback(encodeURIComponent(`/en/invite/${TOKEN}`))).toBe(`/en/invite/${TOKEN}`);
    expect(nextFromCallback("/en")).toBeNull();
    expect(nextFromCallback("https://evil.example/en/inbox")).toBe("/en/inbox");
    expect(nextFromCallback(undefined)).toBeNull();
  });
});

describe("invite tokens and pages", () => {
  it("accepts only 43 URL-safe characters", () => {
    expect(TOKEN).toHaveLength(43);
    expect(isWellFormedInviteToken(TOKEN)).toBe(true);
    expect(isWellFormedInviteToken(TOKEN.slice(1))).toBe(false);
    expect(isWellFormedInviteToken(`${TOKEN.slice(1)}!`)).toBe(false);
    expect(isWellFormedInviteToken(42)).toBe(false);
  });

  it("lets people open an invitation before signing in", () => {
    expect(isPublicPath(`/invite/${TOKEN}`)).toBe(true);
    expect(isPublicPath("/invitex")).toBe(false);
  });
});

describe("magic link per-address limit", () => {
  function fakeDb(counts: number[]): Queryable & { calls: unknown[][] } {
    const calls: unknown[][] = [];
    return {
      calls,
      query: async (_sql: string, params: unknown[]) => {
        calls.push(params);
        return { rows: [{ count: counts.shift() }] };
      },
    };
  }

  it(`allows ${MAGIC_LINK_EMAIL_MAX} links per window, then stops`, async () => {
    const db = fakeDb([1, 2, 3, 4, 5]);
    const results = [];
    for (let i = 0; i < 5; i++) results.push(await takeMagicLinkQuota(db, "Sam@Example.test "));
    expect(results).toEqual([true, true, true, false, false]);
    // Keyed on the normalised address, hashed; the window length is passed to the SQL.
    expect(db.calls[0]?.[1]).toBe(limitKey("sam@example.test"));
    expect(db.calls[0]?.[1]).not.toContain("sam");
    expect(db.calls[0]?.[2]).toBe(MAGIC_LINK_EMAIL_WINDOW_SECONDS);
  });

  it("fails closed when the database does not answer", async () => {
    const broken: Queryable = { query: async () => Promise.reject(new Error("down")) };
    expect(await takeMagicLinkQuota(broken, "sam@example.test")).toBe(false);
    const empty: Queryable = { query: async () => ({ rows: [] }) };
    expect(await takeMagicLinkQuota(empty, "sam@example.test")).toBe(false);
  });

  it("treats addresses that differ only in case or spaces as one", () => {
    expect(limitKey(" SAM@example.TEST")).toBe(limitKey("sam@example.test"));
    expect(limitKey("sam@example.test")).not.toBe(limitKey("sam2@example.test"));
  });
});
