// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology

import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";

import { expect, test, type Browser, type BrowserContext, type Page } from "@playwright/test";

import { auditA11y } from "../utils/axe";
import { asPay, newestMessage, portalLoginLink, signInPay, type PayPerson } from "../utils/pay";
import { AUTH_STATE_DIR, logCheck, SCRATCH_ROOT } from "../utils/report";

// Accessibility of the Pay pages added in M4 and M5 that 19-pay-ux-budgets.spec.ts does
// not cover: axe in English and Arabic, light and dark, on a stack seeded by `make demo`
// (org "demo", scripts/demo_pay.py), against the running native stack (LOCAL_WEB_PORT,
// LOCAL_API_PORT, PAIRLY_OUTBOX_DIR):
//
// * staff pages: Pay settings and the cash forecast, analytics, month end (finance
//   controller), the exceptions queue, purchase orders and reconciliation (AP clerk);
// * the three Pay Home dashboards (AP clerk, approver, finance controller);
// * the Slack connect page (a link without a valid token: the page a person lands on);
// * the vendor portal: the login page (a used link), and signed in as the seeded vendor
//   contact (a magic link requested on the sign-in form, read from the dev outbox): home,
//   an invoice and the details page.
//
// Every violation goes to <scratch>/findings/axe.ndjson and <scratch>/pay-a11y.json, and
// fails the test (soft: every page is still checked). Sessions are the ones 18 and 19
// saved (utils/pay.ts). Nothing here changes data except one portal sign-in.

test.describe.configure({ mode: "serial" });

const OUT = path.join(SCRATCH_ROOT, "pay-a11y.json");
const SHOTS = process.env.PAIRLY_E2E_DEMO_DIR ?? path.join(SCRATCH_ROOT, "m5-a11y");
mkdirSync(SHOTS, { recursive: true });
mkdirSync(AUTH_STATE_DIR, { recursive: true });
// The vendor contact `make demo` gave portal access (scripts/demo_pay.py seed_portal).
const PORTAL_VENDOR = "Seltus IT Solutions LLC";
const PORTAL_EMAIL = "omar.nasser@seltus.example";
const PORTAL_STATE = path.join(AUTH_STATE_DIR, "pay20-portal.json");
const MAIL_WAIT = 150_000; // the worker delivers once a minute

const COMBOS = [
  ["en", "light"],
  ["ar", "light"],
  ["en", "dark"],
  ["ar", "dark"],
] as const;
type Locale = (typeof COMBOS)[number][0];
type Theme = (typeof COMBOS)[number][1];

const results: Record<string, number> = existsSync(OUT) ? (JSON.parse(readFileSync(OUT, "utf8")) as Record<string, number>) : {};

/** Axe on the current page. Soft: a violation is reported, the run goes on. */
async function a11y(page: Page, name: string, locale: Locale, theme: Theme): Promise<void> {
  await page.waitForLoadState("networkidle").catch(() => undefined);
  await expect(page.locator("html")).toHaveAttribute("dir", locale === "ar" ? "rtl" : "ltr");
  const violations = await auditA11y(page, name, locale, theme);
  results[`${name} ${locale} ${theme}`] = violations;
  writeFileSync(OUT, JSON.stringify(results, null, 2));
  expect.soft(violations, `${name} (${locale}, ${theme}) axe violations`).toBe(0);
  if (locale === "ar" && theme === "dark") {
    await page.screenshot({ path: path.join(SHOTS, `${name}-ar-dark.png`), fullPage: false });
  }
}

type Ready = (page: Page) => Promise<void>;
const visible =
  (selector: string): Ready =>
  async (page) => {
    await expect(page.locator(selector).first()).toBeVisible({ timeout: 30_000 });
  };

/** Axe on `pathname` (no locale prefix) in every locale and theme, as `who`. */
async function everyCombo(browser: Browser, who: PayPerson, name: string, pathname: string, ready: Ready): Promise<void> {
  for (const [locale, theme] of COMBOS) {
    const { context, page } = await asPay(browser, who, { theme });
    await page.goto(`/${locale}${pathname}`);
    await ready(page);
    await a11y(page, name, locale, theme);
    await context.close();
  }
}

test("set up: the Pay people sign in", async ({ browser }) => {
  test.setTimeout(300_000);
  for (const who of ["clerk", "approver", "finance", "admin"] as const) await signInPay(browser, who);
});

test("axe: the finance controller's pages", async ({ browser }) => {
  test.setTimeout(420_000);
  await everyCombo(browser, "finance", "pay-settings", "/pay/settings", visible("[data-pay-settings]"));
  await everyCombo(browser, "finance", "pay-analytics", "/pay/analytics", visible("[data-pay-analytics]"));
  await everyCombo(browser, "finance", "pay-month-end", "/pay/month-end", visible("[data-month-end]"));
  await everyCombo(browser, "finance", "pay-forecast", "/pay/forecast", visible("[data-pay-forecast]"));
  logCheck({ name: "pay axe: settings, analytics, month end, forecast", status: "pass" });
});

test("axe: the AP clerk's pages", async ({ browser }) => {
  test.setTimeout(420_000);
  await everyCombo(browser, "clerk", "pay-exceptions", "/pay/exceptions", visible("[data-exceptions]"));
  await everyCombo(browser, "clerk", "pay-purchase-orders", "/pay/purchase-orders", visible("[data-po-list]"));
  await everyCombo(browser, "clerk", "pay-reconciliation", "/pay/reconciliation", visible("[data-reconciliation]"));
  logCheck({ name: "pay axe: exceptions, purchase orders, reconciliation", status: "pass" });
});

test("axe: the three Pay Home dashboards", async ({ browser }) => {
  test.setTimeout(420_000);
  const dashboards = { clerk: "ap_clerk", approver: "approver", finance: "finance_controller" } as const;
  for (const [who, dashboard] of Object.entries(dashboards) as [PayPerson, string][]) {
    await everyCombo(browser, who, `home-${dashboard}`, "", visible(`[data-pay-dashboard="${dashboard}"]`));
  }
  logCheck({ name: "pay axe: AP clerk, approver and finance controller Home", status: "pass" });
});

test("axe: the Slack connect page", async ({ browser }) => {
  test.setTimeout(240_000);
  // The link `/pairly connect` answers with, with a token that is not valid (any real one
  // would link accounts on confirm): the page shows that the link does not work.
  await everyCombo(browser, "admin", "slack-connect", "/integrations/slack/connect#not-a-real-token", async (page) => {
    await expect(page.locator("main")).toBeVisible({ timeout: 30_000 });
    await page.waitForLoadState("networkidle").catch(() => undefined);
  });
  logCheck({ name: "pay axe: Slack connect", status: "pass" });
});

async function portalContext(browser: Browser, theme: Theme): Promise<BrowserContext> {
  return browser.newContext({ storageState: PORTAL_STATE, colorScheme: theme, viewport: { width: 1280, height: 900 } });
}

test("axe: the vendor portal (login, home, invoice, details)", async ({ browser }) => {
  test.setTimeout(720_000);
  // The login page with a link that no longer works: it offers a new one.
  for (const [locale, theme] of COMBOS) {
    const context = await browser.newContext({ colorScheme: theme });
    const page = await context.newPage();
    await page.goto(`/${locale}/vendor-portal/login#vl1.not-a-real-link`);
    await expect(page.locator("[data-vendor-sign-in]")).toBeVisible({ timeout: 30_000 });
    await a11y(page, "vendor-portal-login", locale, theme);
    await context.close();
  }

  // The organisation's portal link, from the vendor page in Pay.
  const { context: staff, page: clerk } = await asPay(browser, "clerk");
  await clerk.goto("/en/pay");
  const vendorLink = clerk.locator("[data-vendor-list] a").filter({ hasText: PORTAL_VENDOR }).first();
  await expect(vendorLink).toBeVisible({ timeout: 30_000 });
  await vendorLink.click();
  await clerk.waitForURL(/\/pay\/vendors\/[0-9a-f-]{36}$/);
  const access = clerk.locator("[data-portal-access]");
  await expect(access).toBeVisible({ timeout: 30_000 });
  const org = /org=([0-9a-f]{32})/.exec((await access.textContent()) ?? "")?.[1];
  expect(org, "the portal link names the organisation").toBeTruthy();
  await staff.close();

  // The vendor asks for a sign-in link and opens it from the email.
  const vendor = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  const page = await vendor.newPage();
  const since = Date.now() - 1000;
  await page.goto(`/en/vendor-portal?org=${org}`);
  await page.locator("[data-vendor-sign-in]").getByRole("textbox").fill(PORTAL_EMAIL);
  await page.locator("[data-vendor-sign-in]").getByRole("button").click();
  await expect(page.locator("[data-link-sent]")).toBeVisible({ timeout: 30_000 });
  const mail = await newestMessage(
    (m) => m.to.some((to) => to.toLowerCase() === PORTAL_EMAIL) && m.html.includes("vendor-portal/login"),
    since,
    MAIL_WAIT,
  );
  await page.goto(portalLoginLink(mail.html));
  await expect(page.locator("[data-vendor-home]")).toBeVisible({ timeout: 30_000 });
  const invoice = ((await page.locator('a[href*="/vendor-portal/invoices/"]').first().getAttribute("href")) ?? "").replace(/^\/(en|ar)/, "");
  expect(invoice).toMatch(/\/vendor-portal\/invoices\//);
  await vendor.storageState({ path: PORTAL_STATE });
  await vendor.close();

  for (const [name, pathname, selector] of [
    ["vendor-portal-home", "/vendor-portal", "[data-vendor-home]"],
    ["vendor-portal-invoice", invoice, "[data-vendor-invoice-detail]"],
    ["vendor-portal-details", "/vendor-portal/details", "[data-vendor-details]"],
  ] as const) {
    for (const [locale, theme] of COMBOS) {
      const context = await portalContext(browser, theme);
      const p = await context.newPage();
      await p.goto(`/${locale}${pathname}`);
      await visible(selector)(p);
      await a11y(p, name, locale, theme);
      await context.close();
    }
  }
  logCheck({ name: "pay axe: vendor portal login, home, invoice, details", status: "pass" });
});
