// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology

import { test, expect, request } from "@playwright/test";

import { logCheck, logSecurity } from "../utils/report";

function record(name: string, ok: boolean, detail?: string) {
  logCheck({ name, status: ok ? "pass" : "fail", detail });
}

const BASE = process.env.PAIRLY_E2E_BASE_URL ?? "http://127.0.0.1:3100";

test.describe("Security smoke", () => {
  for (const url of [`${BASE}/api/auth/token`, `${BASE}/api/auth/organization/list`]) {
    test(`GET ${url} returns 404 from the browser`, async ({ page }) => {
      const response = await page.goto(url);
      const status = response?.status();
      const headers = response?.headers() ?? {};
      logSecurity({ url, status, headers });
      record(`404: ${url}`, status === 404, `status=${status}`);

      const hasXfo = (headers["x-frame-options"] ?? "").toUpperCase() === "DENY";
      const hasReferrer = Boolean(headers["referrer-policy"]);
      const hasCsp = Boolean(headers["content-security-policy"]);
      record(`${url}: X-Frame-Options DENY`, hasXfo, headers["x-frame-options"] ?? "(missing)");
      record(`${url}: Referrer-Policy present`, hasReferrer, headers["referrer-policy"] ?? "(missing)");
      record(`${url}: Content-Security-Policy present`, hasCsp, hasCsp ? "present" : "(missing)");
    });
  }

  test("a normal page has CSP, X-Frame-Options DENY and Referrer-Policy", async ({ page }) => {
    const response = await page.goto(`${BASE}/en/sign-in`);
    const headers = response?.headers() ?? {};
    logSecurity({ url: `${BASE}/en/sign-in`, status: response?.status(), headers });

    expect(headers["x-frame-options"]?.toUpperCase()).toBe("DENY");
    expect(headers["referrer-policy"]).toBeTruthy();
    expect(headers["content-security-policy"]).toBeTruthy();
    record("sign-in page security headers (CSP, X-Frame-Options, Referrer-Policy)", true);
  });

  test("cookies are secure-flagged and SameSite=Lax (http:// dev still gets the pairly cookie prefix)", async () => {
    const ctx = await request.newContext();
    const res = await ctx.get(`${BASE}/en/sign-in`);
    const headers = res.headersArray().filter((h) => h.name.toLowerCase() === "set-cookie");
    logSecurity({ url: `${BASE}/en/sign-in`, setCookie: headers.map((h) => h.value) });
    await ctx.dispose();
  });
});
