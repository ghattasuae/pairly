// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology

import { existsSync, mkdirSync, readFileSync, statSync, writeFileSync } from "node:fs";
import path from "node:path";

import { expect, test, type Browser, type BrowserContext, type Page } from "@playwright/test";

import { auditA11y } from "../utils/axe";
import { extractLink, OUTBOX_DIR, toReachableUrl } from "../utils/mail";
import { AUTH_STATE_DIR, logCheck, SCRATCH_ROOT } from "../utils/report";
import { totpFresh } from "../utils/totp";

// The M3 Hire flows end to end on a fresh `make demo` stack (org "demo",
// scripts/demo_seed.py), against the running native stack (LOCAL_WEB_PORT and
// LOCAL_API_PORT name its ports, PAIRLY_OUTBOX_DIR its outbox):
//
// 1. A hiring manager requests a role, HR (the recruiter) approves it in the inbox and a
//    job is opened from it.
// 2. An offer from a template for the demo candidate in the offer stage: the hiring
//    manager and then HR (the admin; the recruiter submitted it) approve with a
//    two-step sign-in, the recruiter sends it and the candidate accepts in the portal.
// 3. A referee answers a reference request through the emailed link; the recruiter sees
//    the summary.
// 4. The recruiter, hiring manager, interviewer and talent lead (admin) Home dashboards
//    each show their "needs you now" section.
// 5. Hiring analytics, the bias report and the survey results show values or "no data",
//    never invented numbers.
//
// Axe runs on every new page in English and Arabic (light theme); the results go to
// <scratch>/hire-complete.json and the axe log. Staff sign in once each with a magic link
// from the outbox (rate limited per address; sessions are reused for an hour on the same
// stack). The spec changes the seeded data: run it once per `make demo`, one worker.

test.describe.configure({ mode: "serial" });

const ORG = "demo";
const RUN = Date.now().toString(36);
const ADMIN_EMAIL = "demo-admin@example.test";
const RECRUITER_EMAIL = `${ORG}-hire-recruiter@example.test`;
const MANAGER_EMAIL = `${ORG}-hire-manager@example.test`;
const INTERVIEWER_EMAIL = `${ORG}-hire-interviewer@example.test`;
const OFFER_JOB = "backend-engineer"; // the seed leaves cv-0029 in its offer stage
const REQ_TITLE = `Data Platform Engineer ${RUN}`;
const OFFER_TITLE = `Backend Engineer ${RUN}`;
const REFEREE = { name: "Samira Haddad", email: `referee.${RUN}@example.test` };
// The offer email links the offer in the candidate portal.
const OFFER_LINK = /\/portal\/applications\/[0-9a-f-]{36}\/offer/;
const MAIL_WAIT = 150_000; // the worker delivers once a minute: two rounds plus slack
const APPROVAL_WAIT = 120_000;
const OUT = path.join(SCRATCH_ROOT, "hire-complete.json");

mkdirSync(SCRATCH_ROOT, { recursive: true });
mkdirSync(AUTH_STATE_DIR, { recursive: true });

const state = (who: string) => path.join(AUTH_STATE_DIR, `m3-${ORG}-${who}.json`);
const STATES = {
  admin: state("admin"),
  recruiter: state("recruiter"),
  manager: state("manager"),
  interviewer: state("interviewer"),
};
const shared = path.join(AUTH_STATE_DIR, `m3-${ORG}-shared.json`); // between tests

const results: Record<string, unknown> = {};
function record(key: string, value: unknown): void {
  results[key] = value;
  writeFileSync(OUT, JSON.stringify(results, null, 2));
}

interface Shared {
  applicationId?: string;
  candidateEmail?: string;
}
function loadShared(): Shared {
  return existsSync(shared) ? (JSON.parse(readFileSync(shared, "utf8")) as Shared) : {};
}
function saveShared(next: Shared): void {
  writeFileSync(shared, JSON.stringify({ ...loadShared(), ...next }));
}

// ---------------------------------------------------------------- outbox

interface Outboxed {
  id: string;
  to: string[];
  text: string;
  html: string;
}

/** The newest outbox message sent at or after `since` (ms) that passes `match`. */
async function newestMessage(match: (m: Outboxed) => boolean, since = 0, timeoutMs = MAIL_WAIT): Promise<Outboxed> {
  const deadline = Date.now() + timeoutMs;
  const index = path.join(OUTBOX_DIR, "index.jsonl");
  while (Date.now() < deadline) {
    if (existsSync(index)) {
      for (const line of readFileSync(index, "utf8").split("\n").filter(Boolean).reverse()) {
        try {
          const entry = JSON.parse(line) as { id: string };
          if (Number(entry.id.slice(0, 13)) < since) continue;
          const full = JSON.parse(readFileSync(path.join(OUTBOX_DIR, `${entry.id}.json`), "utf8")) as {
            to: string[];
            text: string;
            html: string | null;
          };
          const message = { id: entry.id, to: full.to, text: full.text, html: full.html ?? "" };
          if (match(message)) return message;
        } catch {
          // A line still being written.
        }
      }
    }
    await new Promise((r) => setTimeout(r, 2_000));
  }
  throw new Error(`no matching outbox message in ${OUTBOX_DIR} after ${timeoutMs} ms`);
}

const sentTo = (m: Outboxed, email: string) => m.to.some((to) => to.toLowerCase() === email.toLowerCase());

/** When this stack's outbox got its first message (the seed): older sessions are stale. */
function seededAt(): number {
  const index = path.join(OUTBOX_DIR, "index.jsonl");
  const first = existsSync(index) ? readFileSync(index, "utf8").split("\n")[0] : "";
  try {
    return Number((JSON.parse(first ?? "") as { id: string }).id.slice(0, 13));
  } catch {
    return Date.now();
  }
}

// ---------------------------------------------------------------- staff sessions

/** Sign a seeded staff member in with a magic link from the outbox, turn on two-step
 *  sign-in (approvals and stage decisions need it, and Home asks for it), save the
 *  session. Reused for an hour on the same stack. */
async function signIn(browser: Browser, email: string, file: string): Promise<void> {
  if (existsSync(file) && statSync(file).mtimeMs > seededAt() && Date.now() - statSync(file).mtimeMs < 3_600_000) return;
  const context = await browser.newContext();
  const page = await context.newPage();
  const since = Date.now() - 1000;
  await page.goto("/en/sign-in");
  await page.getByRole("textbox").first().fill(email);
  await page.keyboard.press("Enter");
  await page.waitForURL(/\/check-email/, { timeout: 30_000 });
  const mail = await newestMessage((m) => sentTo(m, email) && m.html.includes("sign-in/confirm"), since, 30_000);
  await page.goto(toReachableUrl(extractLink(mail.html, "sign-in/confirm")));
  await page.getByRole("link", { name: /sign in/i }).click();
  await page.waitForURL((u) => !/sign-in|check-email/.test(u.pathname), { timeout: 30_000 });

  await page.goto("/en/settings/security");
  const turnOn = page.getByRole("button", { name: /turn on/i });
  if (await turnOn.isVisible().catch(() => false)) {
    await turnOn.click();
    await expect(page.locator("code")).toBeVisible();
    const secret = ((await page.locator("code").textContent()) ?? "").replace(/\s+/g, "");
    await page.getByLabel(/code from the app/i).fill(await totpFresh(secret));
    await page.getByRole("button", { name: /confirm and turn on/i }).click();
    await expect(page.getByText(/two-step sign-in is on/i)).toBeVisible({ timeout: 10_000 });
    await page.getByRole("button", { name: /^done$/i }).click();
    // The session picks up the second factor on its next refresh.
    await page.waitForTimeout(20_000);
    await page.waitForLoadState("load");
  }
  await context.storageState({ path: file });
  await context.close();
}

async function as(browser: Browser, file: string): Promise<{ context: BrowserContext; page: Page }> {
  const context = await browser.newContext({ storageState: file, viewport: { width: 1440, height: 1000 } });
  return { context, page: await context.newPage() };
}

// ---------------------------------------------------------------- axe

/** Axe on the current page (light theme). Soft: a violation is reported, the flow goes on. */
async function a11y(page: Page, name: string, locale: "en" | "ar"): Promise<void> {
  await page.waitForLoadState("networkidle").catch(() => undefined);
  if (locale === "ar") await expect(page.locator("html")).toHaveAttribute("dir", "rtl");
  const violations = await auditA11y(page, name, locale, "light");
  record(`axe ${name} ${locale}`, violations);
  expect.soft(violations, `${name} (${locale}) axe violations`).toBe(0);
}

/** Axe on `pathname` in English and Arabic (the path has no locale prefix); ends on English. */
async function a11yBoth(page: Page, name: string, pathname: string, ready: (p: Page) => Promise<void>): Promise<void> {
  for (const locale of ["en", "ar"] as const) {
    await page.goto(`/${locale}${pathname}`);
    await ready(page);
    await a11y(page, name, locale);
  }
  await page.goto(`/en${pathname}`);
  await ready(page);
}

/** Approve the pending item whose row reads `title` in the inbox (a session with the
 *  second factor, so the dialog asks for no code). The approval run may still be
 *  starting: the inbox is reloaded until the item shows. */
async function approveInInbox(page: Page, title: string): Promise<void> {
  const row = page.locator("[data-approval-row]").filter({ hasText: title });
  await expect(async () => {
    await page.goto("/en/inbox");
    await expect(row.first()).toBeVisible({ timeout: 5_000 });
  }).toPass({ timeout: APPROVAL_WAIT, intervals: [3_000] });
  await row.first().click();
  await page.getByRole("button", { name: /^Approve/ }).first().click();
  const dialog = page.getByRole("dialog");
  await expect(dialog).toContainText(title);
  await dialog.getByRole("button", { name: "Approve", exact: true }).click();
  await expect(page.getByRole("status").filter({ hasText: "Approved" }).first()).toBeVisible({ timeout: 15_000 });
}

function isoDate(daysAhead: number): string {
  const d = new Date(Date.now() + daysAhead * 86_400_000);
  return d.toISOString().slice(0, 10);
}

// ---------------------------------------------------------------- tests

test("set up: the admin, recruiter, hiring manager and interviewer sign in with two-step sign-in", async ({ browser }) => {
  test.setTimeout(6 * 60_000);
  await signIn(browser, MANAGER_EMAIL, STATES.manager);
  await signIn(browser, RECRUITER_EMAIL, STATES.recruiter);
  await signIn(browser, ADMIN_EMAIL, STATES.admin);
  await signIn(browser, INTERVIEWER_EMAIL, STATES.interviewer);
  logCheck({ name: "hire complete: staff signed in", status: "pass" });
});

test("1. requisition: the hiring manager requests a role, HR approves it in the inbox, a job opens from it", async ({ browser }) => {
  test.setTimeout(6 * 60_000);
  const manager = await as(browser, STATES.manager);
  const page = manager.page;
  await a11yBoth(page, "requisitions", "/hire/requisitions", async (p) => {
    await expect(p.locator("[data-requisition-form]")).toBeVisible({ timeout: 20_000 });
  });
  const form = page.locator("[data-requisition-form]");
  await form.getByLabel("Job title", { exact: true }).fill(REQ_TITLE);
  await form.getByLabel("Team", { exact: true }).fill("Data");
  await form.getByLabel("Location", { exact: true }).fill("Dubai");
  await form.getByLabel("Yearly budget for the role").fill("300000");
  await form.getByLabel("How many people").fill("1");
  await form.getByLabel("Work model").selectOption("hybrid");
  await form.getByLabel("Start date").fill(isoDate(60));
  await form.getByLabel(/Why the team needs this role/).fill("The data team runs two pipelines with one engineer.");
  await page.locator("[data-requisition-submit]").click();
  const item = page.locator("[data-requisition]").filter({ hasText: REQ_TITLE });
  await expect(item).toBeVisible({ timeout: 20_000 });
  await expect(item.locator("[data-status]")).toHaveAttribute("data-status", "pending_approval");
  await expect(item).toContainText("Waiting for approval");
  const reference = (await item.getAttribute("data-requisition")) ?? "";
  expect(reference).toMatch(/^REQ-[A-Z0-9]{6}$/);
  await manager.context.close();

  // HR (the recruiter) approves it in the inbox.
  const recruiter = await as(browser, STATES.recruiter);
  await approveInInbox(recruiter.page, reference);

  // Approved: the job opens from it (a draft job with the requisition's title).
  const r = recruiter.page;
  const approved = r.locator("[data-requisition]").filter({ hasText: REQ_TITLE });
  await expect(async () => {
    await r.goto("/en/hire/requisitions");
    await expect(approved.locator("[data-status]")).toHaveAttribute("data-status", "approved", { timeout: 5_000 });
  }).toPass({ timeout: APPROVAL_WAIT, intervals: [3_000] });
  await approved.locator("[data-open-job]").click();
  await r.waitForURL(/\/hire\/jobs\/[0-9a-f-]{36}$/, { timeout: 30_000 });
  await expect(r.locator('[data-field="title"]')).toHaveValue(REQ_TITLE, { timeout: 20_000 });
  await expect(r.locator("[data-job-status]")).toHaveAttribute("data-job-status", "draft");
  await r.goto("/en/hire/requisitions");
  await expect(approved.getByRole("link", { name: "View the job" })).toBeVisible();
  record("requisition", { reference, title: REQ_TITLE, job: "opened" });
  logCheck({ name: "hire complete 1: requisition requested, approved in the inbox, job opened", status: "pass", detail: reference });
  await recruiter.context.close();
});

test("2. offer: from a template, approved by the hiring manager and HR, accepted by the candidate in the portal", async ({ browser }) => {
  test.setTimeout(12 * 60_000);
  // The recruiter finds the demo candidate in the offer stage and builds the offer.
  const recruiter = await as(browser, STATES.recruiter);
  const r = recruiter.page;
  await r.goto("/en/hire");
  await r.locator(`[data-job-link="${OFFER_JOB}"]`).click();
  await r.waitForURL(/\/hire\/jobs\/[0-9a-f-]{36}\/pipeline/);
  const card = r.locator('[data-column="offer"] [data-card]').first();
  await expect(card).toBeVisible({ timeout: 30_000 });
  const applicationId = (await card.getAttribute("data-card")) ?? "";
  expect(applicationId).toMatch(/^[0-9a-f-]{36}$/);
  saveShared({ applicationId });

  const offerPath = `/hire/applications/${applicationId}/offer`;
  await a11yBoth(r, "offer builder", offerPath, async (p) => {
    await expect(p.locator("[data-offer-builder]")).toBeVisible({ timeout: 20_000 });
  });
  // A rerun on the same stack: withdraw the offer an earlier run left open.
  const leftOpen = r.locator(
    ["draft", "pending_approval", "changes_requested", "approved", "sending"].map((x) => `[data-offer="${x}"]`).join(", "),
  );
  while ((await leftOpen.count()) > 0) {
    await leftOpen.first().getByRole("button", { name: "Withdraw" }).click();
    await expect(r.locator("[data-new-offer]")).toBeVisible({ timeout: 20_000 });
  }
  const form = r.locator("[data-new-offer]");
  await expect(form).toBeVisible();
  await form.getByLabel("Template").selectOption("standard");
  await expect(form.getByLabel("Conditions")).toHaveValue(/Probation period of 6 months/);
  await form.getByLabel("Job title").fill(OFFER_TITLE);
  await form.getByLabel("Start date").fill(isoDate(45));
  await form.getByLabel("Basic salary (monthly)").fill("30000");
  await form.getByLabel("Housing (monthly)").fill("5000");
  await expect(r.locator("[data-annual-total]")).toContainText(/420,000/);
  await r.locator("[data-create-offer]").click();
  const offer = r.locator("[data-offer]").first();
  await expect(offer).toHaveAttribute("data-offer", "draft", { timeout: 20_000 });
  await offer.locator("[data-submit-offer]").click();
  await expect(offer).toHaveAttribute("data-offer", "pending_approval", { timeout: 20_000 });
  // The chain shows once the approval run has started.
  const chain = r.locator("[data-offer]").first().getByRole("list", { name: "Approval chain" });
  await expect(async () => {
    await r.goto(`/en${offerPath}`);
    await expect(chain).toContainText("Hiring manager", { timeout: 5_000 });
  }).toPass({ timeout: APPROVAL_WAIT, intervals: [3_000] });
  await expect(chain).toContainText("HR");
  await recruiter.context.close();

  // The chain: the hiring manager, then HR (the admin: the recruiter submitted it and
  // never approves their own offer). Both signed in with a code.
  for (const who of ["manager", "admin"] as const) {
    const approver = await as(browser, STATES[who]);
    await approveInInbox(approver.page, OFFER_TITLE);
    await approver.context.close();
  }

  // Approved: the recruiter sends it.
  const sender = await as(browser, STATES.recruiter);
  const s = sender.page;
  const sendButton = s.locator("[data-offer] [data-send-offer]").first();
  await expect(async () => {
    await s.goto(`/en${offerPath}`);
    await expect(sendButton).toBeVisible({ timeout: 5_000 });
  }).toPass({ timeout: APPROVAL_WAIT, intervals: [3_000] });
  const since = Date.now() - 1000;
  await sendButton.click();
  await expect(s.locator("[data-offer]").first()).toHaveAttribute("data-offer", "sending", { timeout: 20_000 });
  await sender.context.close();

  // The offer email names the candidate's address; the candidate signs in to the portal.
  const offerMail = await newestMessage((m) => OFFER_LINK.test(m.text) && m.text.includes(applicationId), since);
  const candidateEmail = offerMail.to[0] ?? "";
  expect(candidateEmail).toContain("@");
  saveShared({ candidateEmail });

  const candidate = await browser.newContext({ viewport: { width: 390, height: 844 } });
  const c = await candidate.newPage();
  const linkSince = Date.now() - 1000;
  await c.goto(`/en/portal?org=${ORG}`);
  await c.locator("[data-portal-sign-in] input[type=email]").fill(candidateEmail);
  await c.getByRole("button", { name: "Email me a link" }).click();
  const portalMail = await newestMessage((m) => sentTo(m, candidateEmail) && /\/portal\/login#pl1\./.test(m.text), linkSince);
  const portalLink = portalMail.text.match(/https?:\/\/\S+\/portal\/login#pl1\.\S+/)?.[0] ?? "";
  await c.goto(toReachableUrl(portalLink));
  await c.waitForURL(/\/en\/portal$/, { timeout: 30_000 });
  await c.goto(`/en/portal/applications/${applicationId}`);
  await c.locator("[data-portal-offer-link]").click();
  await c.waitForURL(/\/portal\/applications\/[0-9a-f-]{36}\/offer$/);
  const portalOfferPath = new URL(c.url()).pathname.replace(/^\/en/, "");
  await a11yBoth(c, "portal offer", portalOfferPath, async (p) => {
    await expect(p.locator("[data-offer-answer]")).toBeVisible({ timeout: 20_000 });
  });
  await expect(c.getByText(OFFER_TITLE).first()).toBeVisible();
  await c.locator("[data-confirm-offer]").check();
  await c.locator("[data-accept-offer]").click();
  await expect(c.locator("[data-offer-answered]")).toContainText("You accepted this offer.", { timeout: 20_000 });
  await candidate.close();

  // The hiring team sees the accepted offer.
  const after = await as(browser, STATES.recruiter);
  await after.page.goto(`/en${offerPath}`);
  await expect(after.page.locator("[data-offer]").first()).toHaveAttribute("data-offer", "accepted", { timeout: 20_000 });
  await expect(after.page.locator("[data-offer-status]").first()).toHaveText("Accepted");
  await after.context.close();
  record("offer", { title: OFFER_TITLE, chain: ["hiring manager", "HR (admin)"], candidate: "accepted" });
  logCheck({ name: "hire complete 2: offer from template, approved, accepted in the portal", status: "pass" });
});

test("2b. the pipeline shows the accepted offer", async ({ browser }) => {
  const { applicationId } = loadShared();
  expect(applicationId, "step 2 found the application").toBeTruthy();
  const recruiter = await as(browser, STATES.recruiter);
  const r = recruiter.page;
  await r.goto("/en/hire");
  await r.locator(`[data-job-link="${OFFER_JOB}"]`).click();
  await r.waitForURL(/\/hire\/jobs\/[0-9a-f-]{36}\/pipeline/);
  const card = r.locator(`[data-card="${applicationId}"]`);
  await expect(card).toBeVisible({ timeout: 20_000 });
  const badge = card.locator("[data-offer-state]");
  await expect(badge).toHaveAttribute("data-offer-state", "accepted", { timeout: 20_000 });
  await expect(badge).toHaveText("Offer accepted");
  await recruiter.context.close();
  logCheck({ name: "hire complete 2b: the pipeline card shows the accepted offer", status: "pass" });
});

test("3. reference: the referee answers through the emailed link, the recruiter sees the summary", async ({ browser }) => {
  test.setTimeout(8 * 60_000);
  const { applicationId } = loadShared();
  expect(applicationId, "step 2 found the application").toBeTruthy();
  const refsPath = `/hire/applications/${applicationId}/references`;

  const recruiter = await as(browser, STATES.recruiter);
  const r = recruiter.page;
  await r.goto(`/en${refsPath}`);
  const ask = r.locator("[data-ask-referee]");
  await expect(ask).toBeVisible({ timeout: 20_000 });
  await ask.getByLabel("Referee's name").fill(REFEREE.name);
  await ask.getByLabel("Referee's email").fill(REFEREE.email);
  await ask.getByLabel("How they know the candidate").selectOption("manager");
  const since = Date.now() - 1000;
  await r.locator("[data-send-request]").click();
  await expect(r.locator('[data-reference="requested"]').filter({ hasText: REFEREE.name })).toBeVisible({ timeout: 20_000 });
  await recruiter.context.close();

  const mail = await newestMessage((m) => sentTo(m, REFEREE.email) && /\/reference#rr1\./.test(m.text), since);
  const link = mail.text.match(/https?:\/\/\S+\/reference#rr1\.\S+/)?.[0] ?? "";
  expect(link).not.toBe("");

  const referee = await browser.newContext({ viewport: { width: 390, height: 844 } });
  const f = await referee.newPage();
  await f.goto(toReachableUrl(link));
  await expect(f.locator("[data-reference-form]")).toBeVisible({ timeout: 30_000 });
  await a11y(f, "referee form", "en");
  // The same session in Arabic (the link was used; the session carries on).
  await f.goto("/ar/reference");
  await expect(f.locator("[data-reference-form]")).toBeVisible({ timeout: 30_000 });
  await a11y(f, "referee form", "ar");
  await f.goto("/en/reference");
  const form = f.locator("[data-reference-form]");
  await expect(form).toBeVisible({ timeout: 30_000 });
  await form.locator('[data-question="known_as"] [data-option="manager"]').click();
  await form.locator('[data-question="company"] input').fill("Synthetic Logistics LLC");
  await form.locator('[data-question="candidate_title"] input').fill("Backend Engineer");
  await form.locator('[data-question="worked_from"] input').fill("2021-03");
  await form.locator('[data-question="worked_to"] input').fill("2024-06");
  await form.locator('[data-question="strengths"] textarea').fill("Careful API design and calm incident handling.");
  await form.locator('[data-question="development"] textarea').fill("Could delegate more on larger projects.");
  await form.locator('[data-question="reliability"] [data-option="4"]').click();
  await form.locator('[data-question="would_rehire"] [data-option="yes"]').click();
  await f.locator("[data-submit-reference]").click();
  await expect(f.locator("[data-reference-submitted]")).toContainText(`Thank you, ${REFEREE.name}`, { timeout: 20_000 });
  await referee.close();

  const after = await as(browser, STATES.recruiter);
  await a11yBoth(after.page, "references", refsPath, async (p) => {
    await expect(p.locator("[data-references]")).toBeVisible({ timeout: 30_000 });
  });
  const p = after.page;
  await expect(p.locator('[data-reference="submitted"]').filter({ hasText: REFEREE.name })).toBeVisible();
  const summary = p.locator("[data-reference-summary]");
  await expect(summary).toBeVisible({ timeout: 30_000 });
  await expect(summary).toContainText(/Summarised (by AI )?from the answers/);
  await expect(summary.getByRole("heading", { name: "Strengths" })).toBeVisible();
  await after.context.close();
  record("reference", { referee: "answered", summary: "visible" });
  logCheck({ name: "hire complete 3: reference answered by the referee, summary visible", status: "pass" });
});

test("4. Home dashboards: recruiter, hiring manager, interviewer and talent lead each show what needs them now", async ({ browser }) => {
  test.setTimeout(6 * 60_000);
  const boards = [
    ["recruiter", "recruiter"],
    ["manager", "hiring_manager"],
    ["interviewer", "interviewer"],
    ["admin", "talent"],
  ] as const;
  const seen: Record<string, string> = {};
  for (const [who, key] of boards) {
    const { context, page } = await as(browser, STATES[who]);
    const board = page.locator(`[data-dashboard="${key}"]`);
    await a11yBoth(page, `home ${key}`, "", async (p) => {
      await expect(p.locator(`[data-dashboard="${key}"]`)).toBeVisible({ timeout: 30_000 });
    });
    const needs = board.locator("h3").first();
    await expect(needs).toHaveText(/Nothing needs you right now|\d+ things? needs? you now/);
    seen[key] = (await needs.textContent()) ?? "";
    // Every count shown is the size of a list of records: a number, never a placeholder.
    for (const count of await board.locator("[data-dash-count]").all()) {
      expect(await count.getAttribute("data-dash-count")).toMatch(/^\d+$/);
    }
    await context.close();
  }
  record("dashboards", seen);
  logCheck({ name: "hire complete 4: role dashboards show what needs each person now", status: "pass", detail: JSON.stringify(seen) });
});

test("5. analytics, bias report and survey results show values or no data, never invented numbers", async ({ browser }) => {
  test.setTimeout(6 * 60_000);
  const { context, page } = await as(browser, STATES.admin);

  // Hiring analytics: every metric is a value from the records or "No data yet".
  await a11yBoth(page, "hiring analytics", "/hire/analytics", async (p) => {
    await expect(p.locator("[data-hiring-analytics]")).toBeVisible({ timeout: 30_000 });
  });
  await expect(page.getByText(/^(No applications in this period\.|Based on \d+ applications?\.)$/).first()).toBeVisible();
  const metrics = page.locator("[data-hiring-analytics] [data-metric]");
  expect(await metrics.count()).toBeGreaterThan(0);
  const analytics: Record<string, string> = {};
  for (const metric of await metrics.all()) {
    const key = (await metric.getAttribute("data-metric")) ?? "";
    const text = ((await metric.innerText()) ?? "").replace(/\s+/g, " ");
    expect(text, `metric ${key}`).toMatch(/\d|No data yet|have heard from us|has heard|not heard/);
    analytics[key] = text.slice(0, 120);
  }

  // Bias report: the four-fifths rule and the small sample setting are labelled; every
  // group shows counts with a judgement, or the category says it has no answers.
  await a11yBoth(page, "bias report", "/hire/bias", async (p) => {
    await expect(p.locator("[data-bias-report]")).toBeVisible({ timeout: 30_000 });
  });
  await expect(page.getByText(/four-fifths rule of thumb/)).toBeVisible();
  await expect(page.getByLabel("Small sample under")).toHaveValue(/^\d+$/);
  await expect(page.getByText(/Fisher exact test/)).toBeVisible();
  await expect(page.getByText(/^(No applications in this scope\.|Based on \d+ applications?\.)$/).first()).toBeVisible();
  const judgements = /^(Reference \(highest rate\)|At or above four-fifths|Below four-fifths|Too small to judge|Hidden|Not compared)$/;
  const categories = page.locator("[data-bias-report] [data-category]");
  const bias: Record<string, string> = {};
  for (const category of await categories.all()) {
    const key = (await category.getAttribute("data-category")) ?? "";
    const groups = category.locator("[data-group]");
    if ((await groups.count()) === 0) {
      await expect(category.getByText("No self-identification answers in this scope yet.")).toBeVisible();
      bias[key] = "no answers";
      continue;
    }
    for (const group of await groups.all()) {
      await expect(group.locator("td").last()).toHaveText(judgements);
    }
    bias[key] = `${await groups.count()} group rows`;
  }
  await expect(page.locator("[data-fairness]")).toBeVisible();

  // Survey results: counts, a rate or "No answers".
  await a11yBoth(page, "survey results", "/hire/surveys", async (p) => {
    await expect(p.locator("[data-survey-results]")).toBeVisible({ timeout: 30_000 });
  });
  const survey = page.locator("[data-survey-results]");
  const tiles = survey.locator(":scope > div").first().locator("p.font-display");
  await expect(tiles.nth(0)).toHaveText(/^\d+$/);
  await expect(tiles.nth(1)).toHaveText(/^\d+$/);
  await expect(tiles.nth(2)).toHaveText(/^(\d+%|No answers)$/);
  const surveyCounts = await tiles.allInnerTexts();

  await context.close();
  record("reports", { analytics, bias, survey: surveyCounts });
  logCheck({ name: "hire complete 5: analytics, bias and survey pages show values or no data", status: "pass" });
});
