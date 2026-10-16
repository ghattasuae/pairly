// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology

import { execFileSync } from "node:child_process";
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";

import { expect, test, type Browser, type Page } from "@playwright/test";

import { asPay, newestMessage, portalLoginLink, signInPay, type PayPerson } from "../utils/pay";
import { AUTH_STATE_DIR, logCheck, SCRATCH_ROOT } from "../utils/report";
import { REPO_ROOT } from "../utils/stack";

// M5 acceptance: Pay complete, on a `make demo` stack (org "demo", scripts/demo_pay.py)
// after 18-pay-demo.spec.ts and 19-pay-ux-budgets.spec.ts (their sessions, and the
// invoices 18 delivered), against the running native stack (LOCAL_WEB_PORT,
// LOCAL_API_PORT, PAIRLY_OUTBOX_DIR):
//
// 1. the exceptions queue shows invoices by category, and a drafted message to a vendor
//    goes to another person for approval;
// 2. a vendor contact signs in to the vendor portal from the emailed link, sees the
//    statuses of their invoices and sends a credit note, which accounts payable applies;
// 3. an early-payment discount (the vendor's agreed terms) appears and is taken in a
//    payment run proposal;
// 4. the cash forecast shows weeks;
// 5. reconciliation imports a synthetic bank statement and matches the paid run;
// 6. the month-end report and the generic journal (a step-up session) download, and
//    every journal entry balances;
// 7. the finance controller's Home shows its sections.
//
// One screenshot per step goes to PAIRLY_E2E_DEMO_DIR (default <scratch>/m5-complete).
// The spec changes the seeded data (a message, portal access, a credit note, payment
// terms, a run proposal, a statement): run it once per `make demo`, with one worker.

test.describe.configure({ mode: "serial" });

const SHOTS = process.env.PAIRLY_E2E_DEMO_DIR ?? path.join(SCRATCH_ROOT, "m5-complete");
mkdirSync(SHOTS, { recursive: true });
mkdirSync(AUTH_STATE_DIR, { recursive: true });
const READ_WAIT = 300_000; // the worker reads a document within a minute or two
const MAIL_WAIT = 150_000; // and delivers email once a minute

// The seeded vendor with an approved invoice (scripts/demo_pay.py, synth.pay inv-0037).
const CREDIT_VENDOR = "Malviro Facilities Management LLC";
const CREDIT_INVOICE = "MAL/26/0859";
// A synthetic document the demo has a recorded reading of (synth.pay inv-0096), sent by
// the vendor as the credit note: its total (AED 7691.32) is less than the invoice's.
const CREDIT_DOC = "inv-0096";
const CREDIT_TOTAL = "7691.32";
// Agreed terms with a discount window that is still open for that invoice (issued
// 2026-04-08): 2 percent off when paid within 200 days.
const DISCOUNT_TERMS = "2/200 net 240";

const sharedFile = path.join(AUTH_STATE_DIR, "pay21-shared.json");
interface Shared {
  creditNote?: string;
  creditReference?: string;
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

/** Open a vendor's page in Pay from the vendor list. */
async function openVendor(page: Page, name: string): Promise<void> {
  await page.goto("/en/pay");
  const link = page.locator("[data-vendor-list] a").filter({ hasText: name }).first();
  await expect(link).toBeVisible({ timeout: 30_000 });
  await link.click();
  await page.waitForURL(/\/pay\/vendors\/[0-9a-f-]{36}$/, { timeout: 30_000 });
  await expect(page.locator("[data-vendor-detail]")).toBeVisible({ timeout: 30_000 });
}

/** Wait until one of `people` has an approval whose title contains `title`. */
async function approvalFor(browser: Browser, people: PayPerson[], title: string): Promise<PayPerson> {
  const deadline = Date.now() + 120_000;
  while (Date.now() < deadline) {
    for (const who of people) {
      const { context, page } = await asPay(browser, who);
      await page.goto("/en/inbox");
      const found = page.locator("[data-approval-row]").filter({ hasText: title });
      const seen = await found
        .first()
        .waitFor({ state: "visible", timeout: 5_000 })
        .then(() => true)
        .catch(() => false);
      if (seen) {
        await shot(page, "1-message-approval.png");
        await context.close();
        return who;
      }
      await context.close();
    }
  }
  throw new Error(`no approval "${title}" for ${people.join(" or ")}`);
}

test("set up: the AP clerk, the approver, the finance controller and the admin sign in", async ({ browser }) => {
  test.setTimeout(300_000);
  for (const who of ["clerk", "approver", "finance", "admin"] as const) await signInPay(browser, who);
});

test("1. exceptions by category; a drafted vendor message goes to approval", async ({ browser }) => {
  test.setTimeout(300_000);
  const { context, page } = await asPay(browser, "clerk");
  await page.goto("/en/pay/exceptions");
  const queue = page.locator("[data-exceptions]");
  await expect(queue).toBeVisible({ timeout: 30_000 });
  const items = queue.locator("[data-exception]");
  await expect(items.first()).toBeVisible({ timeout: 30_000 });
  const count = await items.count();
  for (const item of await items.all()) {
    expect(await item.locator("[data-category]").count(), "every exception has a category").toBeGreaterThan(0);
    await expect(item.locator("[data-resolution]").first()).toBeVisible();
  }
  // The category filter narrows the queue to one category.
  const filter = queue.locator("[data-filter]").first();
  const category = (await filter.getAttribute("data-filter")) ?? "";
  await filter.click();
  await expect(async () => {
    for (const item of await queue.locator("[data-exception]").all()) {
      await expect(item.locator(`[data-category="${category}"]`)).toHaveCount(1, { timeout: 2_000 });
    }
  }).toPass({ timeout: 30_000 });
  await shot(page, "1-exceptions.png");
  await page.goto("/en/pay/exceptions");
  await expect(items.first()).toBeVisible({ timeout: 30_000 });

  // A message to the vendor of the first exception whose vendor has a contact on file.
  let reference: string | null = null;
  for (const item of await items.all()) {
    const write = item.getByRole("button", { name: "Write a message" });
    if (!(await write.isVisible())) continue;
    await write.click();
    const composer = item.locator("[data-composer]");
    await expect(composer).toBeVisible();
    const template = composer.getByLabel("Message", { exact: true });
    const option = template.locator("option").filter({ hasText: "to the vendor" }).first();
    if ((await option.count()) === 0) {
      await composer.getByRole("button", { name: "Close" }).click();
      continue;
    }
    await template.selectOption((await option.getAttribute("value")) ?? "");
    await composer.getByRole("button", { name: "Draft" }).click();
    const text = composer.getByLabel("Text");
    const noContacts = composer.locator("[data-no-contacts]");
    await expect(text.or(noContacts).first()).toBeVisible({ timeout: 30_000 });
    if (await noContacts.isVisible()) {
      await composer.getByRole("button", { name: "Close" }).click();
      continue;
    }
    await expect(text).not.toHaveValue("");
    await composer.getByRole("button", { name: "Send for approval" }).click();
    const sent = item.locator('[data-message-sent="awaiting_approval"]');
    await expect(sent).toBeVisible({ timeout: 30_000 });
    await expect(sent).toContainText("is waiting for approval");
    reference = (await item.getAttribute("data-exception")) ?? null;
    await shot(page, "1-message-drafted.png");
    break;
  }
  expect(reference, "an exception with a vendor contact to write to").toBeTruthy();
  await context.close();

  // Nothing is sent until another person approves the exact text: it waits in their inbox.
  const who = await approvalFor(browser, ["approver", "finance"], `Message to the vendor about invoice ${reference}`);
  logCheck({ name: "pay complete 1: exceptions by category, message to approval", status: "pass", detail: `${count} exceptions, approval for ${who}` });
});

test("2. vendor portal: emailed link, invoice statuses, a credit note AP applies", async ({ browser }) => {
  test.setTimeout(READ_WAIT + MAIL_WAIT + 240_000);
  // The credit note document (synthetic, generated like every synth.pay file).
  const creditFile = path.join(SCRATCH_ROOT, `${CREDIT_DOC}-credit-note.pdf`);
  execFileSync(
    "uv",
    [
      "run",
      "--frozen",
      "python",
      "-c",
      `import sys; from synth.pay.dataset import build; open(sys.argv[1], "wb").write(build().doc("${CREDIT_DOC}").render())`,
      creditFile,
    ],
    { cwd: REPO_ROOT, timeout: 120_000 },
  );

  // Accounts payable gives a contact of the vendor portal access: a link by email.
  const clerk = await asPay(browser, "clerk");
  await openVendor(clerk.page, CREDIT_VENDOR);
  const access = clerk.page.locator("[data-portal-access]");
  await expect(access).toBeVisible({ timeout: 30_000 });
  const contact = access.getByLabel("Contact on file");
  const email = (await contact.locator("option").nth(1).getAttribute("value")) ?? "";
  expect(email).toContain("@");
  await contact.selectOption(email);
  const since = Date.now() - 1000;
  await access.getByRole("button", { name: "Give access" }).click();
  await expect(access.getByText(`Access given. A sign-in link went to ${email}.`)).toBeVisible({ timeout: 30_000 });
  await clerk.context.close();
  const mail = await newestMessage(
    (m) => m.to.some((to) => to.toLowerCase() === email.toLowerCase()) && m.html.includes("vendor-portal/login"),
    since,
    MAIL_WAIT,
  );

  // The vendor opens the link: their invoices with their statuses.
  const vendor = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  const page = await vendor.newPage();
  await page.goto(portalLoginLink(mail.html));
  const home = page.locator("[data-vendor-home]");
  await expect(home).toBeVisible({ timeout: 30_000 });
  const invoices = home.locator("[data-vendor-invoice]");
  await expect(invoices.first()).toBeVisible();
  for (const item of await invoices.all()) await expect(item).toHaveAttribute("data-status", /^[a-z_]+$/);
  const target = invoices.filter({ hasText: CREDIT_INVOICE }).first();
  await expect(target).toHaveAttribute("data-status", "approved");
  await shot(page, "2-portal-home.png");

  // A credit note for the approved invoice.
  const submit = home.locator("[data-vendor-submit]");
  await submit.getByRole("radio", { name: "Credit note" }).check();
  const creditFor = submit.getByLabel("Which invoice does this credit note reduce?");
  const option = creditFor.locator("option").filter({ hasText: CREDIT_INVOICE }).first();
  await creditFor.selectOption((await option.getAttribute("value")) ?? "");
  await submit.locator('input[type="file"]').setInputFiles(creditFile);
  await submit.getByRole("button", { name: "Send" }).click();
  // "Received." (or, on a second run against the same stack, "We already have this file.")
  await expect(submit.locator("[data-vendor-notice]")).toContainText(/Reference AP-/, { timeout: 60_000 });
  const creditReference = /Reference (\S+?)\.?$/.exec(((await submit.locator("[data-vendor-notice]").textContent()) ?? "").trim())?.[1] ?? "";
  expect(creditReference).toMatch(/^AP-/);
  await page.reload();
  const credit = home.locator("[data-vendor-invoice]").filter({ hasText: creditReference }).first();
  await expect(credit).toBeVisible({ timeout: 30_000 });
  const creditId = (await credit.getAttribute("data-vendor-invoice")) ?? "";
  saveShared({ creditNote: creditId, creditReference });
  await shot(page, "2-portal-credit-sent.png");

  // Accounts payable reads it, then applies it to the invoice it names.
  const ap = await asPay(browser, "clerk");
  const panel = ap.page.locator("[data-credit-note]");
  // Once read and checked: a person reviews it (needs review: it is a credit note).
  await expect(async () => {
    await ap.page.goto(`/en/pay/invoices/${creditId}`);
    await expect(ap.page.locator("[data-invoice-review]")).toHaveAttribute("data-invoice-review", /^(needs_review|ready)$/, { timeout: 3_000 });
    await expect(panel.getByRole("button", { name: "Apply the credit note" })).toBeEnabled({ timeout: 3_000 });
  }).toPass({ timeout: READ_WAIT, intervals: [10_000] });
  await expect(panel).toContainText(CREDIT_INVOICE);
  await panel.getByLabel("Note (optional)").fill("Matches the services credited by the vendor.");
  await panel.getByRole("button", { name: "Apply the credit note" }).click();
  await expect(ap.page.getByText("Applied. The invoice will be paid that much less.")).toBeVisible({ timeout: 30_000 });
  await expect(panel).toContainText("This credit note is applied to its invoice.");
  await shot(ap.page, "2-credit-applied.png");
  await ap.context.close();

  // The vendor sees it applied, and the invoice to be paid that much less.
  await page.reload();
  await expect(home.locator("[data-vendor-invoice]").filter({ hasText: creditReference }).first()).toHaveAttribute("data-status", "applied", {
    timeout: 30_000,
  });
  const after = home.locator("[data-vendor-invoice]").filter({ hasText: CREDIT_INVOICE }).first();
  await expect(after).toContainText("To be paid after credits");
  await shot(page, "2-portal-credit-applied.png");
  await vendor.close();
  logCheck({ name: "pay complete 2: portal link, statuses, credit note applied", status: "pass", detail: `${creditReference} AED ${CREDIT_TOTAL}` });
});

test("3. an early-payment discount appears and is taken in a payment run", async ({ browser }) => {
  test.setTimeout(240_000);
  const { context, page } = await asPay(browser, "clerk");
  // The vendor's agreed terms offer the discount (the invoice prints only "Net 15").
  await openVendor(page, CREDIT_VENDOR);
  const terms = page.locator("[data-payment-terms]");
  await terms.locator("[data-terms-edit]").click();
  await terms.getByLabel("Agreed terms").fill(DISCOUNT_TERMS);
  await terms.locator("[data-terms-save]").click();
  await expect(terms.locator("[data-terms-value]")).toContainText(DISCOUNT_TERMS, { timeout: 30_000 });

  // The invoice shows the offer.
  await page.goto("/en/pay/invoices?status=approved");
  const row = page.locator("[data-invoice]").filter({ hasText: CREDIT_INVOICE }).first();
  await expect(row).toBeVisible({ timeout: 30_000 });
  await row.click();
  await page.waitForURL(/\/pay\/invoices\/[0-9a-f-]{36}$/);
  await expect(page.locator('[data-early-payment="available"]').first()).toBeVisible({ timeout: 30_000 });
  await shot(page, "3-discount-offer.png");

  // Proposed with the discount taken.
  await page.goto("/en/pay/payment-runs");
  const propose = page.locator("[data-propose]");
  await expect(propose).toBeVisible({ timeout: 30_000 });
  await propose.getByLabel("Cash limit").fill("10000000");
  await propose.getByLabel("Take early-payment discounts").check();
  await propose.getByRole("button", { name: "Preview" }).click();
  const preview = page.locator("[data-preview]");
  await expect(preview.locator("[data-preview-items]")).toContainText(CREDIT_VENDOR, { timeout: 30_000 });
  const item = preview.locator("[data-preview-items] li").filter({ hasText: CREDIT_VENDOR }).first();
  await expect(item).toContainText("discount");
  await expect(preview.locator("[data-savings]")).toBeVisible();
  await shot(page, "3-run-preview.png");
  await page.getByRole("button", { name: "Propose this run" }).click();
  await page.waitForURL(/\/pay\/payment-runs\/[0-9a-f-]{36}$/, { timeout: 30_000 });
  const discounted = page.locator("[data-run-items] [data-discount]").first();
  await expect(discounted).toBeVisible({ timeout: 30_000 });
  const saving = (await discounted.getAttribute("data-discount")) ?? "";
  expect(Number(saving)).toBeGreaterThan(0);
  await shot(page, "3-run-with-discount.png");
  logCheck({ name: "pay complete 3: early-payment discount in a run", status: "pass", detail: `saving ${saving}` });
  await context.close();
});

test("4. the cash forecast shows weeks", async ({ browser }) => {
  test.setTimeout(120_000);
  const { context, page } = await asPay(browser, "finance");
  await page.goto("/en/pay/forecast");
  const view = page.locator("[data-pay-forecast]");
  await expect(view).toBeVisible({ timeout: 30_000 });
  const weeks = view.locator("[data-week]");
  await expect(weeks.first()).toBeVisible({ timeout: 30_000 });
  expect(await weeks.count()).toBeGreaterThan(1);
  await shot(page, "4-forecast.png");
  logCheck({ name: "pay complete 4: cash forecast by week", status: "pass", detail: `${await weeks.count()} weeks` });
  await context.close();
});

test("5. reconciliation imports a synthetic statement and matches the paid run", async ({ browser }) => {
  test.setTimeout(240_000);
  const { context, page } = await asPay(browser, "clerk");
  // The run `make demo` approved, exported and marked paid (not the imported history).
  await page.goto("/en/pay/payment-runs?status=paid");
  const run = page.locator("[data-run-list] a[data-run^='PR-']").first();
  await expect(run).toBeVisible({ timeout: 30_000 });
  const runReference = (await run.getAttribute("data-run")) ?? "";
  await run.click();
  await page.waitForURL(/\/pay\/payment-runs\/[0-9a-f-]{36}$/);
  const rows = page.locator("[data-run-items] [data-run-item]");
  await expect(rows.first()).toBeVisible({ timeout: 30_000 });
  const lines: string[] = ["date,amount,currency,reference,counterparty_iban"];
  const today = new Date().toISOString().slice(0, 10);
  for (const row of await rows.all()) {
    const reference = (await row.getAttribute("data-run-item")) ?? "";
    const money = ((await row.locator("td").nth(4).locator("span").first().textContent()) ?? "").trim();
    const [currency, amount] = money.split(/\s+/);
    const twoPlaces = (amount ?? "").replace(/^(\d+\.\d{2})\d*$/, "$1");
    lines.push(`${today},-${twoPlaces},${currency},Payment ${reference},`);
  }
  // A synthetic statement: one debit per payment of the run, the bank fee is unrelated.
  lines.push(`${today},-25.00,AED,BANK CHARGES,`);
  const statement = path.join(SCRATCH_ROOT, `statement-${runReference}.csv`);
  writeFileSync(statement, `${lines.join("\n")}\n`);
  const payments = lines.length - 2;

  await page.goto("/en/pay/reconciliation");
  const view = page.locator("[data-reconciliation]");
  await expect(view).toBeVisible({ timeout: 30_000 });
  const upload = view.locator("[data-statement-import]");
  await upload.getByLabel("Statement file").setInputFiles(statement);
  await upload.getByRole("button", { name: "Import" }).click();
  await expect(upload.locator("[data-statement-done]")).toBeVisible({ timeout: 60_000 });
  await page.reload();
  const matched = view.locator('[data-matched-lines] [data-statement-line="matched"]');
  await expect(matched).toHaveCount(payments, { timeout: 30_000 });
  await expect(view.locator('[data-unmatched-lines] [data-statement-line="unmatched"]')).toHaveCount(1);
  await shot(page, "5-reconciliation.png");
  logCheck({ name: "pay complete 5: statement imported, paid run matched", status: "pass", detail: `${runReference}: ${payments} matched` });
  await context.close();
});

test("6. month-end report and the generic journal (step-up) download, balanced", async ({ browser }) => {
  test.setTimeout(180_000);
  const { context, page } = await asPay(browser, "finance");
  // This month (the page defaults to the previous one): the demo's paid run and the
  // credit note applied in step 2.
  const month = new Date().toISOString().slice(0, 7);
  await page.goto(`/en/pay/month-end?period=${month}`);
  const view = page.locator("[data-month-end]");
  await expect(view).toBeVisible({ timeout: 30_000 });
  const period = (await view.getAttribute("data-month-end")) ?? "";
  expect(period).toBe(month);
  await expect(view.locator("[data-aging-panel]")).toBeVisible();

  const report = page.waitForEvent("download");
  await view.locator('[data-download="report"]').click();
  const reportFile = await report;
  expect(reportFile.suggestedFilename()).toBe(`month-end-${period}.csv`);
  const reportText = readFileSync((await reportFile.path())!, "utf8");
  expect(reportText.split("\n").length).toBeGreaterThan(2);

  // The journal needs a second factor: the finance controller's session has one.
  const journal = page.waitForEvent("download");
  await view.locator('[data-download="journal"]').click();
  const journalFile = await journal;
  expect(journalFile.suggestedFilename()).toBe(`journal-${period}.csv`);
  const journalPath = path.join(SHOTS, journalFile.suggestedFilename());
  await journalFile.saveAs(journalPath);
  const [header, ...rows] = readFileSync(journalPath, "utf8").trim().split(/\r?\n/);
  const columns = (header ?? "").split(",");
  expect(columns.slice(0, 7)).toEqual(["entry", "date", "journal", "account_code", "cost_centre", "debit", "credit"]);
  const at = (name: string) => columns.indexOf(name);
  const cents = (value: string) => Math.round(Number(value || "0") * 1000);
  const debit = new Map<string, number>();
  const credit = new Map<string, number>();
  const journals = new Set<string>();
  for (const line of rows) {
    // No cell here holds a comma (codes, ISO dates, decimals, ids), except the vendor
    // name, which comes after every column read below.
    const cells = line.split(",");
    const entry = cells[at("entry")] ?? "";
    journals.add(cells[at("journal")] ?? "");
    debit.set(entry, (debit.get(entry) ?? 0) + cents(cells[at("debit")] ?? ""));
    credit.set(entry, (credit.get(entry) ?? 0) + cents(cells[at("credit")] ?? ""));
  }
  expect(debit.size).toBeGreaterThan(0);
  for (const [entry, total] of debit) expect(total, `entry ${entry} balances`).toBe(credit.get(entry));
  expect(journals).toContain("AP_PAYMENT");
  expect(journals).toContain("AP_CREDIT");
  await page.reload();
  await expect(view.locator("[data-journal-exports]")).toBeVisible({ timeout: 30_000 });
  await shot(page, "6-month-end.png");
  logCheck({ name: "pay complete 6: month-end report, balanced journal", status: "pass", detail: `${debit.size} entries, ${[...journals].join(" ")}` });
  await context.close();
});

test("7. the finance controller's Home shows its sections", async ({ browser }) => {
  test.setTimeout(120_000);
  const { context, page } = await asPay(browser, "finance");
  await page.goto("/en");
  const board = page.locator('[data-pay-dashboard="finance_controller"]');
  await expect(board).toBeVisible({ timeout: 30_000 });
  for (const key of ["upcoming_runs", "discounts", "exceptions_trend", "month_end"]) {
    await expect(board.locator(`[data-pay-section="${key}"]`), key).toBeVisible();
  }
  await expect(board.locator("[data-pay-metric]").first()).toBeVisible();
  await shot(page, "7-finance-home.png");
  logCheck({ name: "pay complete 7: finance controller Home", status: "pass" });
  await context.close();
});
