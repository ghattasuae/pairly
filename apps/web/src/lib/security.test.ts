// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
import { describe, expect, it } from "vitest";

import { confirmUrl } from "./auth/config";
import { localeFromCallback } from "./auth/two-factor-gate";
import { buildCsp, documentOriginFrom } from "./csp";
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

  it("allows same-origin workers and the document store origin only", () => {
    const csp = buildCsp({ nonce: "abc", dev: false, https: false, documentOrigin: "https://files.example.com" });
    expect(csp).toContain("worker-src 'self'");
    expect(csp).toContain("connect-src 'self' https://files.example.com");
    expect(csp).toContain("img-src 'self' blob: data: https://files.example.com");
    expect(buildCsp({ nonce: "abc", dev: false, https: false })).toContain("connect-src 'self';");
  });
});

describe("documentOriginFrom", () => {
  it("accepts exact origins only", () => {
    expect(documentOriginFrom("https://files.example.com", false)).toBe("https://files.example.com");
    expect(documentOriginFrom("https://files.example.com/", false)).toBe("https://files.example.com");
    expect(documentOriginFrom("https://files.example.com/bucket", false)).toBeNull();
    expect(documentOriginFrom("https://*.example.com", false)).toBeNull();
    expect(documentOriginFrom("javascript:alert(1)", false)).toBeNull();
    expect(documentOriginFrom(undefined, false)).toBeNull();
  });

  it("defaults to the compose MinIO port in development", () => {
    expect(documentOriginFrom(undefined, true)).toBe("http://localhost:9000");
    expect(documentOriginFrom("", true, "9100")).toBe("http://localhost:9100");
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
    expect(isPublicPath("/approvals/link")).toBe(true);
    expect(isPublicPath("/approvals")).toBe(false);
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
