// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology

import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";

import { expect, test, type Locator, type Page } from "@playwright/test";

import { auditA11y } from "../utils/axe";
import { asPay, signInPay } from "../utils/pay";
import { AUTH_STATE_DIR, logCheck, SCRATCH_ROOT } from "../utils/report";

// Pay UX budgets (brief 5.3, UX acceptance targets) and accessibility on a stack seeded
// by `make demo` (org "demo", scripts/demo_pay.py), against the running native stack
// (LOCAL_WEB_PORT and LOCAL_API_PORT name its ports, PAIRLY_OUTBOX_DIR its outbox):
//
// | Task                                    | Budget |
// | Approve an invoice from mobile          | 2 taps |
//
// Counting rules as in 15-hire-ux-budgets.spec.ts: clicks (taps), command keys and wall
// time, failing over budget; typing is content and reported apart. The approver opens
// the item from the notification (its link is the approval in the inbox), so the taps
// counted are the ones in Pairly: approve, then confirm. Results:
// <scratch>/pay-ux-budgets.json.
//
// Axe runs on every Pay page in English and Arabic (light theme): the invoice list, an
// invoice review, the vendors, a vendor, a bank-detail change ticket, the payment runs,
// the purchase orders and the vendor onboarding form (from a new invite link). Results
// go to the same file and the axe log; violations are soft failures.
//
// Sessions are the ones 18-pay-demo.spec.ts saved (utils/pay.ts), so a run right after
// it signs nobody in again (the magic-link request is rate limited per address). The
// spec approves the seeded invoice waiting for the approver and creates one vendor
// invite: run it once per `make demo`, with one worker.

test.describe.configure({ mode: "serial" });

const OUT = path.join(SCRATCH_ROOT, "pay-ux-budgets.json");
mkdirSync(AUTH_STATE_DIR, { recursive: true });
const PHONE = { width: 375, height: 812 };

type Result = Record<string, unknown>;
const results: Record<string, Result | number> = existsSync(OUT)
  ? (JSON.parse(readFileSync(OUT, "utf8")) as Record<string, Result | number>)
  : {};
function record(key: string, value: Result | number): void {
  results[key] = value;
  writeFileSync(OUT, JSON.stringify(results, null, 2));
}

/** Counts every tap and command key of one task, and its wall time. */
class Budget {
  clicks = 0;
  keys = 0;
  typed = 0;
  private readonly started = Date.now();

  constructor(private readonly name: string) {}

  async tap(target: Locator): Promise<void> {
    this.clicks += 1;
    await target.tap();
  }

  done(budget: { clicks: number; keys: number }, label: string): void {
    const ms = Date.now() - this.started;
    const pass = this.clicks <= budget.clicks && this.keys <= budget.keys;
    record(this.name, { clicks: this.clicks, keys: this.keys, typed: this.typed, ms, budget: label, pass });
    logCheck({
      name: `pay ux budget: ${this.name}`,
      status: pass ? "pass" : "fail",
      detail: `${this.clicks} taps, ${this.keys} keys, ${this.typed} typed, ${ms} ms (budget ${label})`,
    });
    expect(this.clicks, `${this.name}: taps`).toBeLessThanOrEqual(budget.clicks);
    expect(this.keys, `${this.name}: keys`).toBeLessThanOrEqual(budget.keys);
  }
}

test("set up: the approver and the AP clerk sign in", async ({ browser }) => {
  test.setTimeout(180_000);
  await signInPay(browser, "approver");
  await signInPay(browser, "clerk");
});

test("approve an invoice from mobile: 2 taps", async ({ browser }) => {
  test.setTimeout(150_000);
  // The approver, signed in with a code before counting, on a 375 px touch screen. The
  // seed leaves one invoice waiting for them (scripts/demo_pay.py, inv-0047).
  const { context, page } = await asPay(browser, "approver", { viewport: PHONE, touch: true });
  const rows = page.locator("[data-approval-row]").filter({ hasText: /Invoice / });
  await expect(async () => {
    await page.goto("/en/inbox");
    await expect(rows.first()).toBeVisible({ timeout: 3_000 });
  }).toPass({ timeout: 90_000, intervals: [3_000] });
  const id = await rows.first().getAttribute("data-approval-row");
  expect(id).toBeTruthy();
  // The notification's link: the approval in the inbox.
  await page.goto(`/en/inbox?item=${id}`);
  const approve = page.getByRole("button", { name: /^Approve/ });
  await expect(approve).toBeVisible({ timeout: 20_000 });
  // The context is visible before approving: the invoice evidence (risk, findings).
  await expect(page.locator('[data-pay-evidence="invoice"]').first()).toBeVisible({ timeout: 20_000 });
  const budget = new Budget("approve an invoice from mobile");
  await budget.tap(approve);
  const dialog = page.getByRole("dialog");
  await expect(dialog).toBeVisible();
  await budget.tap(dialog.getByRole("button", { name: "Approve", exact: true }));
  await expect(page.getByRole("status").filter({ hasText: "Approved" }).first()).toBeVisible({ timeout: 20_000 });
  budget.done({ clicks: 2, keys: 0 }, "2 taps");
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  expect(overflow, "no horizontal scroll at 375 px").toBeLessThanOrEqual(0);
  await page.screenshot({ path: path.join(SCRATCH_ROOT, "pay-approve-375.png"), fullPage: false });
  await context.close();
});

/** Axe on the current page (light theme). Soft: a violation is reported, the run goes on. */
async function a11y(page: Page, name: string, locale: "en" | "ar"): Promise<void> {
  await page.waitForLoadState("networkidle").catch(() => undefined);
  if (locale === "ar") await expect(page.locator("html")).toHaveAttribute("dir", "rtl");
  const violations = await auditA11y(page, name, locale, "light");
  record(`axe ${name} ${locale}`, violations);
  expect.soft(violations, `${name} (${locale}) axe violations`).toBe(0);
}

/** Axe on `pathname` (no locale prefix) in English and Arabic once `ready` holds. */
async function a11yBoth(page: Page, name: string, pathname: string, ready: (p: Page) => Promise<void>): Promise<void> {
  for (const locale of ["en", "ar"] as const) {
    await page.goto(`/${locale}${pathname}`);
    await ready(page);
    await a11y(page, name, locale);
  }
}

const href = async (page: Page, selector: string): Promise<string> => {
  const link = page.locator(selector).first();
  await expect(link).toBeVisible({ timeout: 30_000 });
  return ((await link.getAttribute("href")) ?? "").replace(/^\/(en|ar)/, "");
};

test("axe: every Pay page in English and Arabic", async ({ browser }) => {
  test.setTimeout(360_000);
  const { context, page } = await asPay(browser, "clerk");
  const visible = (selector: string) => async (p: Page) => {
    await expect(p.locator(selector).first()).toBeVisible({ timeout: 30_000 });
  };

  await a11yBoth(page, "pay-invoices", "/pay/invoices", visible("[data-invoice-list]"));
  const invoice = await href(page, "[data-invoice-list] a[data-invoice]");
  await a11yBoth(page, "pay-invoice-review", invoice, visible("[data-invoice-review]"));

  await a11yBoth(page, "pay-vendors", "/pay", visible("[data-vendor-list]"));
  const vendor = await href(page, '[data-vendor-list] a[href*="/pay/vendors/"]');
  await a11yBoth(page, "pay-vendor-detail", vendor, visible("[data-vendor-detail]"));

  await page.goto("/en/pay/bank-changes");
  await page.locator("[data-open-only]").uncheck();
  const ticket = await href(page, '[data-change-list] a[href*="/pay/bank-changes/"]');
  await a11yBoth(page, "pay-bank-change", ticket, visible("[data-bank-change]"));

  await a11yBoth(page, "pay-payment-runs", "/pay/payment-runs", visible("[data-run-list]"));
  await a11yBoth(page, "pay-purchase-orders", "/pay/purchase-orders", visible("[data-po-list]"));

  // The onboarding form: a new single-use invite link from the clerk, opened by a
  // signed-out visitor (the link becomes a session cookie, so Arabic reloads it).
  await page.goto("/en/pay/invites");
  const form = page.locator("[data-invite-form]");
  await expect(form).toBeVisible({ timeout: 30_000 });
  await form.getByLabel("Company (optional)").fill("Synthetic Supplies Trading LLC");
  await form.locator("[data-create-invite]").click();
  const linkBox = page.locator("[data-invite-link] p.font-mono");
  await expect(linkBox).toBeVisible({ timeout: 30_000 });
  const link = new URL(((await linkBox.textContent()) ?? "").trim());
  await context.close();

  const vendorVisitor = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  const visitor = await vendorVisitor.newPage();
  await visitor.goto(`/en/vendor-onboarding${link.hash}`);
  await expect(visitor.locator("[data-onboarding-form]")).toBeVisible({ timeout: 30_000 });
  await a11y(visitor, "pay-onboarding-form", "en");
  await visitor.goto("/ar/vendor-onboarding");
  await expect(visitor.locator("[data-onboarding-form]")).toBeVisible({ timeout: 30_000 });
  await a11y(visitor, "pay-onboarding-form", "ar");
  await visitor.screenshot({ path: path.join(SCRATCH_ROOT, "pay-onboarding-ar.png"), fullPage: false });
  await vendorVisitor.close();
  logCheck({ name: "pay axe: every Pay page en and ar", status: "pass" });
});
