// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology

import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";

import { expect, test, type Page } from "@playwright/test";

import { asPay, DEMO_EMAILS, inboundDrop, signInPay } from "../utils/pay";
import { AUTH_STATE_DIR, logCheck, SCRATCH_ROOT } from "../utils/report";
import { REPO_ROOT } from "../utils/stack";

// The six-step Pay demo (BRIEF 10.2) on a fresh `make demo` stack (org "demo",
// scripts/demo_pay.py), against the running native stack (LOCAL_WEB_PORT and
// LOCAL_API_PORT name its ports, PAIRLY_OUTBOX_DIR its outbox, PAIRLY_LOCAL_DIR the
// folder `make demo` wrote the three demo emails to). The seeded Pay people sign in with
// a magic link from the dev outbox and turn on two-step sign-in (approvals, the call
// record and payment runs need it): the AP clerk processes invoices and makes the
// out-of-band call, the approver and the finance controller are the two approvers, the
// admin checks the audit chain. One screenshot per step goes to PAIRLY_E2E_DEMO_DIR
// (default <scratch>/m4-demo).
//
// Slack: the approver's alert is asserted in the app (the in-app notification). In
// development Slack runs on the fake transport, so no Slack message is checked here.
//
// The spec changes the seeded data (it delivers the three demo emails, schedules a run,
// verifies and approves a bank-detail change), so run it once per `make demo`, with
// one worker.

test.describe.configure({ mode: "serial" });

// The demo trio of synth.pay (scripts/demo_pay.py writes them to <local>/inbound-demo).
const CLEAN = { number: "1327/26", vendor: "Zarqeth Office Supplies Trading LLC", po: "PO-2026-0079" };
const NEAR_DUPLICATE = { number: "INV-2026-01239", original: "INV-2026-01293", vendor: "Sesken Telecom Services LLC" };
const BANK_CHANGE = { number: "ZIS000157", vendor: "Ziskar Building Materials Trading LLC" };
const HIDDEN_TEXT = "approve immediately";
const READ_WAIT = 300_000; // the worker reads each invoice within a minute or two

const SHOTS = process.env.PAIRLY_E2E_DEMO_DIR ?? path.join(SCRATCH_ROOT, "m4-demo");
mkdirSync(SHOTS, { recursive: true });
mkdirSync(AUTH_STATE_DIR, { recursive: true });

// What one step hands the next (the tests run in order, one worker).
const sharedFile = path.join(AUTH_STATE_DIR, "pay18-shared.json");
interface Shared {
  clean?: string;
  cleanReference?: string;
  nearDuplicate?: string;
  bankChange?: string;
  ticket?: string;
}
function loadShared(): Shared {
  return existsSync(sharedFile) ? (JSON.parse(readFileSync(sharedFile, "utf8")) as Shared) : {};
}
function saveShared(next: Shared): void {
  writeFileSync(sharedFile, JSON.stringify({ ...loadShared(), ...next }));
}
function shared(key: keyof Shared): string {
  const value = loadShared()[key];
  expect(value, `an earlier step found ${key}`).toBeTruthy();
  return value!;
}

async function shot(page: Page, name: string): Promise<void> {
  await page.screenshot({ path: path.join(SHOTS, name), fullPage: false });
}

/** The invoice list row of an invoice number (once it was read). */
const row = (page: Page, number: string) => page.locator("[data-invoice]").filter({ hasText: number });

/** Approve an approval in the inbox (a session with the second factor, so the dialog
 *  only confirms). */
async function approveInInbox(page: Page, approvalId: string): Promise<void> {
  await page.goto(`/en/inbox?item=${approvalId}`);
  const approve = page.getByRole("button", { name: /^Approve/ });
  await expect(approve).toBeVisible({ timeout: 30_000 });
  await approve.click();
  const dialog = page.getByRole("dialog");
  await expect(dialog).toBeVisible();
  await dialog.getByRole("button", { name: "Approve", exact: true }).click();
  await expect(page.getByRole("status").filter({ hasText: "Approved" }).first()).toBeVisible({ timeout: 30_000 });
}

test("set up: the AP clerk, the approver, the finance controller and the admin sign in", async ({ browser }) => {
  test.setTimeout(300_000);
  // Cooling-off: the demo keeps the organisation's default (24 hours, set by
  // scripts/demo_pay.py; /pay/settings shows it). With 0 hours the change would
  // activate inside the final approval and skip the state step 5 shows; so the spec
  // leaves it as seeded.
  for (const who of ["clerk", "approver", "finance", "admin"] as const) await signInPay(browser, who);
});

test("1. three demo emails arrive at the inbound address: three new invoices", async ({ browser }) => {
  test.setTimeout(READ_WAIT + 120_000);
  for (const file of DEMO_EMAILS) expect(existsSync(file), `${file} (written by make demo)`).toBe(true);
  const { context, page } = await asPay(browser, "clerk");
  await page.goto("/en/pay/invoices");
  await expect(page.locator("[data-status-filter]")).toBeVisible({ timeout: 30_000 });
  const before = await page.locator("[data-invoice]").count();

  // `make inbound-drop FILE=...` per email, as the demo's next steps say.
  for (const file of DEMO_EMAILS) {
    const out = inboundDrop(file);
    expect(out).not.toMatch(/refused|error/i);
  }

  // The worker reads each one; the list then names its number and vendor.
  await expect(async () => {
    await page.goto("/en/pay/invoices");
    for (const invoice of [CLEAN, NEAR_DUPLICATE, BANK_CHANGE]) {
      await expect(row(page, invoice.number)).toHaveCount(1, { timeout: 2_000 });
    }
  }).toPass({ timeout: READ_WAIT, intervals: [10_000] });
  expect(await page.locator("[data-invoice]").count()).toBe(before + 3);
  for (const invoice of [CLEAN, NEAR_DUPLICATE, BANK_CHANGE]) {
    const item = row(page, invoice.number);
    await expect(item).toContainText(invoice.vendor);
    await expect(item).toContainText("Email");
  }
  const href = async (number: string) => (await row(page, number).getAttribute("href")) ?? "";
  saveShared({
    clean: await href(CLEAN.number),
    cleanReference: (await row(page, CLEAN.number).getAttribute("data-invoice")) ?? "",
    nearDuplicate: await href(NEAR_DUPLICATE.number),
    bankChange: await href(BANK_CHANGE.number),
  });
  await shot(page, "1-invoices.png");
  logCheck({ name: "pay demo 1: three emails, three invoices", status: "pass", detail: `${before} -> ${before + 3}` });
  await context.close();
});

test("2. invoice 1: read with evidence, matched to its PO, approved by the rule, scheduled", async ({ browser }) => {
  test.setTimeout(300_000);
  const invoiceUrl = shared("clean");
  const { context, page } = await asPay(browser, "clerk");
  await page.goto(invoiceUrl);
  const review = page.locator("[data-invoice-review]");
  await expect(review).toHaveAttribute("data-invoice-review", "ready", { timeout: 30_000 });
  // Extracted fields with their confidence, and where each was read on the page.
  await expect(page.locator('[data-field="invoice_number"]')).toContainText(CLEAN.number);
  await expect(page.locator('[data-field="po_number"]')).toContainText(CLEAN.po);
  await expect(page.locator('[data-field="invoice_number"]')).toContainText(/Confidence|\d+%/);
  await expect(page.locator("[data-highlight]").first()).toBeAttached({ timeout: 30_000 });
  await expect(page.locator("[data-invoice-checks]")).toBeVisible();
  await expect(page.locator('[data-checks-state="current"]')).toBeVisible();
  await shot(page, "2-extracted.png");

  // Matched: the PO is on file with its goods receipt, and this invoice is counted on it.
  await page.goto("/en/pay/purchase-orders");
  const po = page.locator(`[data-po="${CLEAN.po}"]`);
  await expect(po).toBeVisible({ timeout: 30_000 });
  await expect(po).toContainText("3-way");
  await expect(po.locator("td").nth(6)).not.toHaveText("0"); // goods receipts
  await expect(po.locator("td").nth(7)).not.toHaveText("0"); // invoices matched to it
  await shot(page, "2-purchase-order.png");

  // Send for approval: the rule approves it (matched, low risk, AED under 10,000).
  await page.goto(invoiceUrl);
  await page.locator("[data-submit]").getByRole("button", { name: "Send for approval" }).click();
  await expect(async () => {
    await page.goto(invoiceUrl);
    await expect(page.locator('[data-approved="rule"]')).toBeVisible({ timeout: 3_000 });
  }).toPass({ timeout: 120_000, intervals: [5_000] });
  await expect(page.locator('[data-approved="rule"]')).toContainText("Approved by a rule");
  await shot(page, "2-approved-by-rule.png");

  // Scheduled: a payment run proposal (AED) that includes it.
  const reference = shared("cleanReference");
  await page.goto("/en/pay/payment-runs");
  const propose = page.locator("[data-propose]");
  await expect(propose).toBeVisible({ timeout: 30_000 });
  await propose.getByLabel("Cash limit").fill("10000000");
  await propose.getByRole("button", { name: "Preview" }).click();
  await expect(page.locator("[data-preview-items]")).toContainText(reference, { timeout: 30_000 });
  await page.getByRole("button", { name: "Propose this run" }).click();
  await page.waitForURL(/\/pay\/payment-runs\/[0-9a-f-]{36}$/, { timeout: 30_000 });
  await expect(page.locator('[data-run-status="awaiting_approval"]').first()).toBeVisible({ timeout: 30_000 });
  await page.goto(invoiceUrl);
  await expect(page.locator("[data-invoice-review]")).toHaveAttribute("data-invoice-review", "scheduled", { timeout: 30_000 });
  await expect(page.locator("[data-payment-run]")).toBeVisible();
  await shot(page, "2-scheduled.png");
  logCheck({ name: "pay demo 2: extracted, matched, approved by rule, scheduled", status: "pass", detail: reference });
  await context.close();
});

test("3. invoice 2: a near duplicate, the original side by side", async ({ browser }) => {
  test.setTimeout(120_000);
  const { context, page } = await asPay(browser, "clerk");
  await page.goto(shared("nearDuplicate"));
  await expect(page.locator("[data-invoice-review]")).toHaveAttribute("data-invoice-review", "needs_review", { timeout: 30_000 });
  await expect(page.locator('[data-finding="near_duplicate"]').first()).toBeVisible();
  const compare = page.locator("[data-compare-panel]");
  await expect(compare).toBeVisible();
  await expect(compare).toContainText("Possible duplicate");
  const number = compare.locator('[data-compare-row="invoice_number"]').first();
  await expect(number).toHaveAttribute("data-same", "false");
  await expect(number.locator("td").nth(0)).toContainText(NEAR_DUPLICATE.number);
  await expect(number.locator("td").nth(1)).toContainText(NEAR_DUPLICATE.original);
  // Everything else is the same as the original (vendor, date, total).
  await expect(compare.locator('[data-compare-row="total"]').first()).toHaveAttribute("data-same", "true");
  await compare.scrollIntoViewIfNeeded();
  await shot(page, "3-near-duplicate.png");
  logCheck({ name: "pay demo 3: near duplicate with the original", status: "pass" });
  await context.close();
});

test("4. invoice 3: blocked, bank-change ticket opened, vendor on hold, lookalike and hidden text shown", async ({ browser }) => {
  test.setTimeout(150_000);
  const { context, page } = await asPay(browser, "clerk");
  await page.goto(shared("bankChange"));
  await expect(page.locator("[data-invoice-review]")).toHaveAttribute("data-invoice-review", "needs_review", { timeout: 30_000 });
  // New bank details, a lookalike sender domain, the hidden instruction.
  await expect(page.locator('[data-finding="bank_mismatch"]').first()).toBeVisible();
  await expect(page.locator('[data-finding="sender_lookalike_domain"]').first()).toBeVisible();
  const notice = page.locator("[data-guardrail-notice]").first();
  await expect(notice).toBeVisible({ timeout: 30_000 });
  await expect(notice).toContainText(HIDDEN_TEXT);
  await expect(notice.locator("[data-guardrail-removed]").first()).toContainText("Pairly removed this text before the AI read it");
  await shot(page, "4-blocked.png");

  // The vendor: payments on hold for the open bank-detail change.
  await page.locator("[data-invoice-review]").getByRole("link", { name: BANK_CHANGE.vendor }).click();
  await page.waitForURL(/\/pay\/vendors\/[0-9a-f-]{36}$/);
  await expect(page.locator('[data-hold="bank_change"]').first()).toBeVisible({ timeout: 30_000 });
  const tickets = page.locator("[data-vendor-tickets]");
  await expect(tickets.locator('[data-ticket-status="awaiting_verification"]')).toBeVisible();
  await shot(page, "4-vendor-hold.png");

  // The ticket the check opened, waiting for the call back.
  await tickets.locator('a[href*="/pay/bank-changes/"]').first().click();
  await page.waitForURL(/\/pay\/bank-changes\/[0-9a-f-]{36}$/);
  saveShared({ ticket: new URL(page.url()).pathname });
  await expect(page.locator("[data-change-progress]")).toHaveAttribute("data-change-progress", "awaiting_verification");
  await expect(page.locator("[data-bank-change]")).toContainText(/invoice/i);
  await expect(page.locator('[data-hold="bank_change"]').first()).toBeVisible();
  await shot(page, "4-ticket.png");
  logCheck({ name: "pay demo 4: blocked, ticket, hold, lookalike, hidden text", status: "pass" });
  await context.close();
});

test("4. the approver is alerted when invoice 3 is blocked", async ({ browser }) => {
  test.setTimeout(150_000);
  const ticketPath = shared("ticket");
  // The urgent alert the invoice check sent when it opened the ticket: in the bell, and
  // its link opens the ticket (Slack is the fake transport in dev; email goes to the
  // dev outbox).
  const { context, page } = await asPay(browser, "approver");
  const bell = page.getByRole("button", { name: /^Notifications/ });
  const alert = page.getByRole("menuitem").filter({ hasText: "are on hold" });
  await expect(async () => {
    await page.goto("/en/inbox");
    await bell.click();
    await expect(alert.first()).toBeVisible({ timeout: 3_000 });
  }).toPass({ timeout: 120_000, intervals: [5_000] });
  await expect(alert.first()).toContainText(/Bank-detail change BCT-/);
  await expect(alert.first()).not.toContainText(/[A-Z]{2}\d{2}[A-Z0-9]{8,}/); // never a full IBAN
  await shot(page, "4-approver-alert.png");
  await alert.first().click();
  await page.waitForURL((url) => url.pathname === ticketPath, { timeout: 30_000 });
  await expect(page.locator("[data-change-progress]")).toHaveAttribute("data-change-progress", "awaiting_verification");
  logCheck({ name: "pay demo 4: approver alerted, the alert opens the ticket", status: "pass" });
  await context.close();
});

test("5. out-of-band call by the verifier, two approvers, then cooling-off", async ({ browser }) => {
  test.setTimeout(300_000);
  const ticketUrl = shared("ticket");

  // The AP clerk calls the number on file (never one from the request) and attests it.
  const clerk = await asPay(browser, "clerk");
  await clerk.page.goto(ticketUrl);
  const checklist = clerk.page.locator("[data-verification-checklist]");
  await expect(checklist).toBeVisible({ timeout: 30_000 });
  await expect(checklist.locator("[data-phone-on-file]")).toBeVisible();
  await expect(checklist.locator("[data-never-request-number]")).toBeVisible();
  await checklist.locator('[data-result="confirmed"]').click();
  await checklist.getByRole("textbox").fill("Called the number on file; the finance manager confirmed the new account.");
  await checklist.locator("[data-attest]").check();
  await checklist.locator("[data-record-call]").click();
  await expect(clerk.page.locator('[data-ticket-status="awaiting_approval"]').first()).toBeVisible({ timeout: 30_000 });
  await expect(clerk.page.locator('[data-call="confirmed"]')).toBeVisible();
  await shot(clerk.page, "5-call-recorded.png");
  // The approval the verified change waits for.
  const inbox = clerk.page.locator("[data-open-inbox]");
  await expect(inbox).toBeVisible({ timeout: 60_000 });
  const approvalId = ((await inbox.getAttribute("href")) ?? "").split("/").pop() ?? "";
  expect(approvalId).toMatch(/^[0-9a-f-]{36}$/);
  await clerk.context.close();

  // The approver's alert: the in-app notification (Slack is the fake transport in dev).
  const approver = await asPay(browser, "approver");
  const bell = approver.page.getByRole("button", { name: /^Notifications/ });
  const alert = approver.page.getByRole("menuitem").filter({ hasText: `Approval needed: Bank details change: ${BANK_CHANGE.vendor}` });
  await expect(async () => {
    await approver.page.goto("/en/inbox");
    await bell.click();
    await expect(alert.first()).toBeVisible({ timeout: 3_000 });
  }).toPass({ timeout: 120_000, intervals: [5_000] });
  await shot(approver.page, "5-approver-alert.png");
  await approver.page.keyboard.press("Escape");

  // Dual approval: the approver, then the finance controller (two different people,
  // both with a two-step sign-in session).
  await approveInInbox(approver.page, approvalId);
  await approver.context.close();
  const finance = await asPay(browser, "finance");
  await approveInInbox(finance.page, approvalId);
  await finance.context.close();

  // Cooling-off: the new details wait, payments stay on hold.
  const check = await asPay(browser, "clerk");
  await expect(async () => {
    await check.page.goto(ticketUrl);
    await expect(check.page.locator('[data-ticket-status="cooling_off"]').first()).toBeVisible({ timeout: 3_000 });
  }).toPass({ timeout: 90_000, intervals: [5_000] });
  await expect(check.page.locator("[data-cooling-off]")).toContainText("cooling-off period");
  const steps = check.page.locator("[data-approval-step]");
  await expect(steps).toHaveCount(2);
  for (const step of await steps.all()) await expect(step).toHaveAttribute("data-step-status", "approved");
  await expect(check.page.locator('[data-hold="bank_change"]').first()).toBeVisible();
  await shot(check.page, "5-cooling-off.png");
  logCheck({ name: "pay demo 5: call back, dual approval, cooling-off", status: "pass" });
  await check.context.close();
});

test("6. Pay analytics: every number has a value or says no data", async ({ browser }) => {
  test.setTimeout(150_000);
  const { context, page } = await asPay(browser, "finance");
  await page.goto("/en/pay/analytics");
  const view = page.locator("[data-pay-analytics]");
  await expect(view).toBeVisible({ timeout: 30_000 });
  const tiles = view.locator("[data-metric]");
  expect(await tiles.count()).toBeGreaterThan(8);
  for (const tile of await tiles.all()) {
    const key = (await tile.getAttribute("data-metric")) ?? "";
    const shown = (await tile.locator("[data-value]").count()) + (await tile.locator("[data-no-data]").count());
    expect(shown, `${key} shows a value or no data`).toBeGreaterThan(0);
  }
  // The demo's blocked invoice is a fraud flag, and its records open the invoices.
  const flags = view.locator('[data-metric="fraud_flags"]');
  await expect(flags.locator("[data-value]")).toBeVisible();
  await flags.locator("summary").click();
  const record = flags.locator('a[href*="/pay/invoices/"]').first();
  await expect(record).toBeVisible();
  await shot(page, "6-analytics.png");
  await record.click();
  await page.waitForURL(/\/pay\/invoices\/[0-9a-f-]{36}$/, { timeout: 30_000 });
  logCheck({ name: "pay demo 6: Pay analytics", status: "pass", detail: `${await tiles.count()} numbers` });
  await context.close();
});

test("6. invoice counts, the eval report and the audit chain", async ({ browser }) => {
  test.setTimeout(240_000);
  const { context, page } = await asPay(browser, "clerk");
  await page.goto("/en/pay/invoices");
  const filter = page.locator("[data-status-filter]");
  await expect(filter).toBeVisible({ timeout: 30_000 });
  const all = Number(((await filter.getByRole("link").first().locator("span").textContent()) ?? "0").trim());
  expect(all).toBeGreaterThan(3);
  await expect(filter.getByRole("link", { name: /^Needs review \d+$/ })).toBeVisible();
  await expect(filter.getByRole("link", { name: /^Scheduled \d+$/ })).toBeVisible();
  await shot(page, "6-invoice-counts.png");
  await context.close();

  // The eval report (BRIEF 10.2 step 6) is the committed evals/report.md, written by
  // `make evals`; its Pay section covers the validation and fraud checks.
  const report = readFileSync(path.join(REPO_ROOT, "evals", "report.md"), "utf8");
  expect(report).toContain("## Pay validation and fraud checks");

  const admin = await asPay(browser, "admin");
  await admin.page.goto("/en/audit");
  await admin.page.getByRole("button", { name: "Check the chain" }).click();
  await expect(admin.page.getByText(/The chain is intact/)).toBeVisible({ timeout: 120_000 });
  await shot(admin.page, "6-audit.png");
  logCheck({ name: "pay demo 6: counts, eval report, audit chain", status: "pass", detail: `${all} invoices` });
  await admin.context.close();
});
