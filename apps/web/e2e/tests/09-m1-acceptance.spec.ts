// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology

import { execFileSync } from "node:child_process";
import { mkdirSync } from "node:fs";
import path from "node:path";

import { expect, test, type Browser, type BrowserContext, type Page } from "@playwright/test";

import { clearMailbox, extractLink, toReachableUrl, waitForMessageTo, waitForMessageWith } from "../utils/mailpit";
import { AUTH_STATE_DIR, logCheck, SCRATCH_ROOT } from "../utils/report";
import { makeArgs, REPO_ROOT } from "../utils/stack";
import { totpFresh } from "../utils/totp";

// M1 acceptance (the milestone gate), end to end in a real browser against the compose
// stack (`PROFILES="web scan" make dev`), independent of the other specs:
//
// 1. a new organisation (`make bootstrap-org`) seeded with the expense example
//    (`make demo-expense`): policies, recorded model responses, three people;
// 2. the approver, the finance controller and the admin accept their invitations and turn
//    on two-step sign-in (deciding and exporting need a code sign-in);
// 3. the submitter (employee) uploads three receipts from the Expenses page: a clean cafe
//    receipt, a stationery receipt with hidden white text and a hotel receipt over 500 AED;
// 4. the approver sees them in the inbox with evidence and guardrail highlights (and on
//    the document page), approves the clean one and rejects the hidden-text one with a
//    reason;
// 5. the hotel receipt takes two steps: the approver approves from the signed link in the
//    approval email (read from Mailpit), then the finance controller from theirs;
// 6. the decision viewer shows the records, and the audit viewer checks the chain against
//    a freshly signed anchor (`make audit-anchor-now`).
//
// Screenshots: $PAIRLY_E2E_M1_DIR, else <PAIRLY_E2E_SCRATCH>/m1-acceptance.

test.describe.configure({ mode: "serial" });

const RUN = Date.now().toString(36);
const SLUG = `m1-${RUN}`;
const ADMIN_EMAIL = `admin+${RUN}@test.example`;
const SHOTS = process.env.PAIRLY_E2E_M1_DIR ?? path.join(SCRATCH_ROOT, "m1-acceptance");
mkdirSync(SHOTS, { recursive: true });

const RECEIPTS = path.join(REPO_ROOT, "synth", "expense", "receipts");
const CLEAN = { file: "receipt-al-noor-cafe.pdf", merchant: "Al Noor Cafe", note: "Team lunch" };
const HIDDEN = { file: "receipt-office-hub-stationery.pdf", merchant: "Office Hub Stationery", note: "Printer paper" };
const HOTEL = { file: "receipt-desert-palm-hotel.pdf", merchant: "Desert Palm Hotel", note: "Client visit" };
const REJECT_REASON = "The receipt carries hidden text that tries to steer the review. Please resubmit the original.";

type Who = "admin" | "submitter" | "approver" | "finance";
const people: Record<Who, { email: string; invite: string; totp: string }> = {
  admin: { email: ADMIN_EMAIL, invite: "", totp: "" },
  submitter: { email: `${SLUG}-expense-submitter@example.test`, invite: "", totp: "" },
  approver: { email: `${SLUG}-expense-approver@example.test`, invite: "", totp: "" },
  finance: { email: `${SLUG}-expense-finance@example.test`, invite: "", totp: "" },
};
const statePath = (who: Who) => path.join(AUTH_STATE_DIR, `m1-${RUN}-${who}.json`);

async function shot(page: Page, name: string): Promise<void> {
  await page.screenshot({ path: path.join(SHOTS, `${name}.png`), fullPage: true });
}

function make(target: string, ...vars: string[]): string {
  return execFileSync("make", ["-s", target, ...vars, ...makeArgs()], { cwd: REPO_ROOT, encoding: "utf8" });
}

/** Accept the invitation over a magic link; with `totp`, turn on two-step sign-in. */
async function onboard(browser: Browser, who: Who, totp: boolean): Promise<void> {
  const person = people[who];
  const context = await browser.newContext();
  const page = await context.newPage();
  await clearMailbox();
  await page.goto(toReachableUrl(person.invite));
  await page.getByRole("button", { name: /email me a sign-in link/i }).click();
  await page.waitForURL(/\/check-email/);
  const mail = await waitForMessageTo(person.email);
  await page.goto(toReachableUrl(extractLink(mail.HTML, "sign-in/confirm")));
  await page.getByRole("link", { name: /sign in/i }).click();
  await page.waitForURL(/\/invite/);
  await page.getByRole("button", { name: /accept and join/i }).click();
  await page.waitForURL((u) => !u.pathname.includes("/invite"), { timeout: 20_000 });

  if (totp) {
    await page.goto("/en/settings/security");
    await page.getByRole("button", { name: /turn on/i }).click();
    await expect(page.locator("code")).toBeVisible();
    person.totp = ((await page.locator("code").textContent()) ?? "").replace(/\s+/g, "");
    await page.getByLabel(/code from the app/i).fill(await totpFresh(person.totp));
    await page.getByRole("button", { name: /confirm and turn on/i }).click();
    await expect(page.getByText(/two-step sign-in is on/i)).toBeVisible({ timeout: 10_000 });
    await page.getByRole("button", { name: /^done$/i }).click();
    // Turning it on counts as a code sign-in for this session, so no second magic link
    // (magic links are limited to 5 in 5 minutes). The pause keeps the next person's
    // two-factor calls under that endpoint's limit (5 a minute).
    await page.waitForTimeout(20_000);
  }
  await page.waitForLoadState("load");
  await context.storageState({ path: statePath(who) });
  await context.close();
}

async function as(browser: Browser, who: Who): Promise<{ context: BrowserContext; page: Page }> {
  const context = await browser.newContext({ storageState: statePath(who), viewport: { width: 1440, height: 1000 } });
  return { context, page: await context.newPage() };
}

const inboxRow = (page: Page, merchant: string) => page.locator("[data-approval-row]").filter({ hasText: merchant });

test("set up: organisation, expense seed, four people", async ({ browser }) => {
  test.setTimeout(240_000);
  const boot = make("bootstrap-org", `NAME=M1 Acceptance ${RUN}`, `SLUG=${SLUG}`, `ADMIN_EMAIL=${ADMIN_EMAIL}`);
  people.admin.invite = boot.match(/https?:\/\/\S+\/invite#token=\S+/)?.[0] ?? "";
  expect(people.admin.invite, boot).not.toBe("");

  const seed = make("demo-expense", `SLUG=${SLUG}`);
  expect(seed).toContain("published approval_rules expense.note version 1");
  expect(seed).toMatch(/recorded model responses: 3 imported/);
  for (const who of ["submitter", "approver", "finance"] as const) {
    const block = seed.slice(seed.indexOf(people[who].email));
    people[who].invite = block.match(/https?:\/\/\S+\/invite#token=\S+/)?.[0] ?? "";
    expect(people[who].invite, seed).not.toBe("");
  }
  logCheck({ name: "m1: bootstrap-org and demo-expense", status: "pass", detail: SLUG });

  // Deciders first, so the approval rules find them when the notes arrive.
  await onboard(browser, "approver", true);
  await onboard(browser, "finance", true);
  await onboard(browser, "admin", true);
  await onboard(browser, "submitter", false);
  logCheck({ name: "m1: four people onboarded (three with two-step sign-in)", status: "pass" });
});

test("submitter uploads three receipts from the Expenses page", async ({ browser }) => {
  test.setTimeout(180_000);
  const { context, page } = await as(browser, "submitter");
  await page.goto("/en/expense");
  await expect(page.getByRole("heading", { name: "Expenses", level: 1 })).toBeVisible();
  // An employee holds no approval permission: no inbox in the navigation.
  await expect(page.getByRole("link", { name: /^Inbox/ })).toHaveCount(0);
  for (const receipt of [CLEAN, HIDDEN, HOTEL]) {
    const submit = page.getByRole("button", { name: "Submit", exact: true });
    await expect(submit).toBeEnabled();
    await page.getByLabel("Receipt", { exact: true }).setInputFiles(path.join(RECEIPTS, receipt.file));
    await page.getByLabel("Note (optional)").fill(receipt.note);
    await submit.click();
    // Accepted: the form resets (the toast of the previous receipt may still be showing).
    await expect(page.getByLabel("Note (optional)")).toHaveValue("", { timeout: 30_000 });
    await expect(submit).toBeEnabled();
  }
  // Scanned (ClamAV), parsed, guardrails, extraction (recorded), rules, risk: then a person.
  await expect(async () => {
    await page.reload();
    await expect(page.getByText("Waiting for approval")).toHaveCount(3, { timeout: 2_000 });
  }).toPass({ timeout: 120_000, intervals: [2_000] });
  for (const receipt of [CLEAN, HIDDEN, HOTEL]) await expect(page.getByText(receipt.merchant)).toBeVisible();
  await shot(page, "01-submitter-expenses-waiting");
  logCheck({ name: "m1: three receipts submitted and waiting for approval", status: "pass" });
  await context.close();
});

test("approver: inbox with evidence and guardrail highlights, approve clean, reject hidden text", async ({ browser }) => {
  test.setTimeout(180_000);
  const { context, page } = await as(browser, "approver");
  await page.goto("/en/inbox");
  await expect(page.getByRole("heading", { name: "Inbox", level: 1 })).toBeVisible();
  for (const receipt of [CLEAN, HIDDEN, HOTEL]) await expect(inboxRow(page, receipt.merchant)).toBeVisible();
  await shot(page, "02-approver-inbox");

  // The clean receipt: evidence boxes on the receipt, no guardrail finding.
  await inboxRow(page, CLEAN.merchant).click();
  await expect(page.getByRole("heading", { level: 2, name: new RegExp(CLEAN.merchant) })).toBeVisible();
  await expect(page.locator('[data-highlight^="evidence:"]').first()).toBeAttached({ timeout: 30_000 });
  await expect(page.locator('[data-highlight^="finding:"]')).toHaveCount(0);
  await shot(page, "03-inbox-clean-evidence");

  // The hidden-text receipt: the white-on-white line is shown to the reviewer, boxed.
  await inboxRow(page, HIDDEN.merchant).click();
  await expect(page.getByRole("heading", { level: 2, name: new RegExp(HIDDEN.merchant) })).toBeVisible();
  await expect(page.locator('[data-highlight^="finding:"]').first()).toBeAttached({ timeout: 30_000 });
  await expect(page.locator('[data-highlight^="evidence:"]').first()).toBeAttached();
  await shot(page, "04-inbox-hidden-text-guardrail");

  // The document page for the hidden-text receipt: the receipt with where each value was
  // read. (It takes guardrail findings from a risk record about the document; the expense
  // workflow's risk record is about the note, so here they show in the inbox only.)
  await page.getByRole("link", { name: "Open the document on its own page" }).click();
  await page.waitForURL(/\/documents\//);
  await expect(page.locator('[data-highlight^="field:"]').first()).toBeAttached({ timeout: 30_000 });
  await expect(page.locator("[data-page] canvas").first()).toBeVisible({ timeout: 30_000 });
  await shot(page, "05-document-viewer-hidden-text");
  logCheck({ name: "m1: evidence and guardrail highlights in the inbox and the document viewer", status: "pass" });

  // Approve the clean receipt (A, then confirm).
  await page.goto("/en/inbox");
  await inboxRow(page, CLEAN.merchant).click();
  await expect(page.getByRole("button", { name: /^Approve/ })).toBeVisible();
  await page.keyboard.press("a");
  const approveDialog = page.getByRole("dialog");
  await expect(approveDialog).toContainText(CLEAN.merchant);
  await shot(page, "06-inbox-approve-dialog");
  await approveDialog.getByRole("button", { name: "Approve", exact: true }).click();
  await expect(page.getByRole("status").filter({ hasText: "Approved" }).first()).toBeVisible({ timeout: 20_000 });

  // Reject the hidden-text receipt with a reason (R, reason, confirm).
  await page.goto("/en/inbox");
  await inboxRow(page, HIDDEN.merchant).click();
  await expect(page.getByRole("button", { name: /^Reject/ })).toBeVisible();
  await page.keyboard.press("r");
  const rejectDialog = page.getByRole("dialog");
  const confirm = rejectDialog.getByRole("button", { name: "Reject", exact: true });
  await expect(confirm).toBeDisabled();
  await rejectDialog.getByLabel("Reason").fill(REJECT_REASON);
  await shot(page, "07-inbox-reject-dialog");
  await confirm.click();
  await expect(page.getByRole("status").filter({ hasText: "Rejected" }).first()).toBeVisible({ timeout: 20_000 });
  await page.goto("/en/inbox?status=rejected");
  await inboxRow(page, HIDDEN.merchant).click();
  await expect(page.getByText(REJECT_REASON)).toBeVisible();
  await shot(page, "08-inbox-rejected-with-reason");
  logCheck({ name: "m1: approved the clean receipt, rejected the hidden-text receipt with a reason", status: "pass" });
  await context.close();
});

test("hotel receipt: two steps, each approved from the signed link in the approval email", async ({ browser }) => {
  test.setTimeout(240_000);
  for (const [who, step] of [
    ["approver", "09"],
    ["finance", "10"],
  ] as const) {
    // The approval email (delivered by the worker) carries Approve then Reject links.
    const mail = await waitForMessageWith(people[who].email, [HOTEL.merchant, "/approvals/link#"], 150_000);
    const link = toReachableUrl(extractLink(mail.HTML, "/approvals/link#"));
    const { context, page } = await as(browser, who);
    await page.goto(link);
    await expect(page.getByRole("heading", { name: "Approve this item" })).toBeVisible({ timeout: 20_000 });
    expect(new URL(page.url()).hash).toBe("");
    await expect(page.getByText(HOTEL.merchant).first()).toBeVisible();
    await shot(page, `${step}-${who}-email-link-ready`);
    await page.getByRole("button", { name: "Approve now" }).click();
    await expect(page.getByRole("heading", { name: "Done" })).toBeVisible({ timeout: 20_000 });
    await shot(page, `${step}-${who}-email-link-done`);
    await context.close();
    logCheck({ name: `m1: ${who} approved the hotel receipt from the email link`, status: "pass" });
  }

  const { context, page } = await as(browser, "submitter");
  await expect(async () => {
    await page.goto("/en/expense");
    await expect(page.getByText("Approved", { exact: true })).toHaveCount(2, { timeout: 2_000 });
    await expect(page.getByText("Rejected", { exact: true })).toHaveCount(1, { timeout: 2_000 });
  }).toPass({ timeout: 60_000, intervals: [2_000] });
  await shot(page, "11-submitter-expenses-decided");
  await context.close();
});

test("decision viewer shows the records, audit viewer verifies the chain and anchors", async ({ browser }) => {
  test.setTimeout(180_000);
  const { context, page } = await as(browser, "approver");
  await page.goto("/en/inbox?status=approved");
  await inboxRow(page, CLEAN.merchant).click();
  const records = page.locator('a[href*="/decisions/dec_"]');
  await expect(records.first()).toBeVisible({ timeout: 20_000 });
  const count = await records.count();
  expect(count).toBeGreaterThanOrEqual(3);
  await records.last().click();
  await page.waitForURL(/\/decisions\/dec_/);
  await expect(page.getByText("Decision record").first()).toBeVisible();
  await expect(page.getByText("Unchanged since it was recorded")).toBeVisible();
  await shot(page, "12-decision-viewer");
  logCheck({ name: `m1: decision viewer (${count} records linked from the approval)`, status: "pass" });
  await context.close();

  // Sign the chain head now (normally nightly), then check the chain as the admin.
  const anchored = make("audit-anchor-now");
  expect(anchored).toContain('"finished": true');
  const admin = await as(browser, "admin");
  await admin.page.goto(`/en/audit?entity_type=expense_note`);
  await expect(admin.page.getByRole("table")).toBeVisible({ timeout: 20_000 });
  await expect(admin.page.getByText("expense.note.awaiting_approval").first()).toBeVisible();
  await admin.page.getByRole("button", { name: "Check the chain" }).click();
  await expect(admin.page.getByText(/The chain is intact/)).toBeVisible({ timeout: 60_000 });
  await expect(admin.page.getByText("Matches the signed anchors.")).toBeVisible();
  await expect(admin.page.getByText(/Latest anchor at event/)).toBeVisible();
  await shot(admin.page, "13-audit-viewer-chain-and-anchors");
  logCheck({ name: "m1: audit chain intact and matches the signed anchors", status: "pass" });
  await admin.context.close();
});
