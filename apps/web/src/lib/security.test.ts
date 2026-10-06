// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
import { describe, expect, it } from "vitest";

import { confirmUrl } from "./auth/config";
import { localeFromCallback } from "./auth/two-factor-gate";
import { buildCsp } from "./csp";
import { escapeHtml } from "./email";
import { isPublicPath } from "./public-paths";

describe("buildCsp", () => {
  it("is strict in production", () => {
    const csp = buildCsp({ nonce: "abc", dev: false, https: true });
    expect(csp).toContain("script-src 'self' 'nonce-abc' 'strict-dynamic'");
    expect(csp).not.toContain("unsafe-eval");
    expect(csp).toContain("frame-ancestors 'none'");
    expect(csp).toContain("object-src 'none'");
    expect(csp).toContain("upgrade-insecure-requests");
  });

  it("allows eval only in development and never upgrades plain http", () => {
    const csp = buildCsp({ nonce: "abc", dev: true, https: false });
    expect(csp).toContain("'unsafe-eval'");
    expect(csp).not.toContain("upgrade-insecure-requests");
  });
});

describe("isPublicPath", () => {
  it("lets sign-in steps through and guards everything else", () => {
    expect(isPublicPath("/sign-in")).toBe(true);
    expect(isPublicPath("/sign-in/confirm")).toBe(true);
    expect(isPublicPath("/two-factor")).toBe(true);
    expect(isPublicPath("/")).toBe(false);
    expect(isPublicPath("/sign-inx")).toBe(false);
    expect(isPublicPath("/settings/security")).toBe(false);
  });
});

describe("magic link URL", () => {
  it("puts the token in the fragment, never the query", () => {
    const url = new URL(confirmUrl("http://localhost:3000", "ar", "tok123"));
    expect(url.pathname).toBe("/ar/sign-in/confirm");
    expect(url.search).toBe("");
    expect(url.hash).toBe("#token=tok123");
  });
});

describe("localeFromCallback", () => {
  it("reads the locale prefix of the callback path", () => {
    expect(localeFromCallback("/ar")).toBe("ar");
    expect(localeFromCallback(encodeURIComponent("/ar/inbox"))).toBe("ar");
    expect(localeFromCallback("/xx/inbox")).toBe("en");
    expect(localeFromCallback(undefined)).toBe("en");
  });
});

describe("escapeHtml", () => {
  it("escapes every HTML special character", () => {
    expect(escapeHtml(`<a href="x">'&'</a>`)).toBe("&lt;a href=&quot;x&quot;&gt;&#39;&amp;&#39;&lt;/a&gt;");
  });
});
