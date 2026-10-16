// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology

import { existsSync, mkdirSync, readFileSync, statSync, writeFileSync } from "node:fs";
import path from "node:path";

import { expect, test, type Browser, type BrowserContext, type Locator, type Page } from "@playwright/test";

import { applyApi } from "../utils/hire";
import { extractLink, OUTBOX_DIR, toReachableUrl } from "../utils/mail";
import { AUTH_STATE_DIR, logCheck, SCRATCH_ROOT } from "../utils/report";
import { totpFresh } from "../utils/totp";

// Hire UX budgets (brief 5.3, UX acceptance targets) on a stack seeded by `make demo`
// (org "demo", scripts/demo_seed.py), against the running native stack (LOCAL_WEB_PORT
// and LOCAL_API_PORT name its ports, PAIRLY_OUTBOX_DIR its outbox):
//
// | Task                                                          | Budget          |
// | Find a past candidate by skill                                | 1 search        |
// | Hiring manager reviews a shortlist of 5 and records decisions | under 5 minutes |
// | Approve an offer or rejection message from mobile             | 2 taps          |
//
// Each task counts clicks (taps), command keys and wall time, and fails over budget.
// Typing a reason is content, not a command: it is reported separately as `typed`.
// Results: <scratch>/hire-ux-budgets.json.
//
// M3: "Interviewer opens scorecard from invite and submits: under 3 minutes, no more than
// 1 login step" is measured in 16-hire-interviews.spec.ts.
//
// The spec changes the seeded data (five new shortlisted applications, their decisions
// and an approval), so run it once per `make demo`, with one worker.

test.describe.configure({ mode: "serial" });

// From scripts/demo_seed.py.
const ORG = "demo";
const MANAGER_EMAIL = `${ORG}-hire-manager@example.test`;
const RECRUITER_EMAIL = `${ORG}-hire-recruiter@example.test`;
const JOB = "backend-engineer-payments";
const JOB_TITLE = "Backend Engineer, Payments";
const TALENT_SKILL = "gRPC"; // TALENT_SKILL: on the CV of the seeded talent pool candidate

const RUN = Date.now().toString(36);
const OUT = path.join(SCRATCH_ROOT, "hire-ux-budgets.json");
mkdirSync(AUTH_STATE_DIR, { recursive: true });
const managerState = path.join(AUTH_STATE_DIR, `ux15-${ORG}-manager.json`);
const recruiterState = path.join(AUTH_STATE_DIR, `ux15-${ORG}-recruiter.json`);
const shortlistFile = path.join(AUTH_STATE_DIR, `ux15-${ORG}-shortlist.json`);
const PHONE = { width: 375, height: 812 };
const FIVE_MINUTES = 5 * 60_000;

type Result = { clicks: number; keys: number; typed: number; searches?: number; ms: number; budget: string; pass: boolean };
const results: Record<string, Result> = existsSync(OUT) ? (JSON.parse(readFileSync(OUT, "utf8")) as Record<string, Result>) : {};

interface Shortlist {
  url: string;
  references: string[];
}

/** Counts every click (tap), command key and search of one task, and its wall time. */
class Budget {
  clicks = 0;
  keys = 0;
  typed = 0;
  searches = 0;
  private readonly started = Date.now();

  constructor(
    private readonly name: string,
    private readonly page: Page,
  ) {}

  async click(target: Locator): Promise<void> {
    this.clicks += 1;
    await target.click();
  }

  async tap(target: Locator): Promise<void> {
    this.clicks += 1;
    await target.tap();
  }

  async press(key: string): Promise<void> {
    this.keys += 1;
    await this.page.keyboard.press(key);
  }

  /** Submit a search: one search, whatever key or button sends it. */
  async search(key: string): Promise<void> {
    this.searches += 1;
    await this.press(key);
  }

  /** Text the person writes (content, not a command). */
  async write(target: Locator, text: string): Promise<void> {
    this.typed += text.length;
    await target.fill(text);
  }

  done(budget: { clicks?: number; keys?: number; searches?: number; ms?: number }, label: string): void {
    const ms = Date.now() - this.started;
    const pass =
      (budget.clicks === undefined || this.clicks <= budget.clicks) &&
      (budget.keys === undefined || this.keys <= budget.keys) &&
      (budget.searches === undefined || this.searches <= budget.searches) &&
      (budget.ms === undefined || ms < budget.ms);
    results[this.name] = {
      clicks: this.clicks,
      keys: this.keys,
      typed: this.typed,
      ...(budget.searches !== undefined ? { searches: this.searches } : {}),
      ms,
      budget: label,
      pass,
    };
    writeFileSync(OUT, JSON.stringify(results, null, 2));
    logCheck({
      name: `hire ux budget: ${this.name}`,
      status: pass ? "pass" : "fail",
      detail: `${this.clicks} clicks, ${this.keys} keys, ${this.typed} typed, ${ms} ms (budget ${label})`,
    });
    if (budget.clicks !== undefined) expect(this.clicks, `${this.name}: clicks`).toBeLessThanOrEqual(budget.clicks);
    if (budget.keys !== undefined) expect(this.keys, `${this.name}: keys`).toBeLessThanOrEqual(budget.keys);
    if (budget.searches !== undefined) expect(this.searches, `${this.name}: searches`).toBeLessThanOrEqual(budget.searches);
    if (budget.ms !== undefined) expect(ms, `${this.name}: time`).toBeLessThan(budget.ms);
  }
}

interface Outboxed {
  id: string;
  to: string[];
  html: string;
}

/** The newest outbox message sent after `since` (ms) that passes `match`. */
async function newestMessage(match: (m: Outboxed) => boolean, since: number, timeoutMs = 30_000): Promise<Outboxed> {
  const deadline = Date.now() + timeoutMs;
  const index = path.join(OUTBOX_DIR, "index.jsonl");
  while (Date.now() < deadline) {
    if (existsSync(index)) {
      const lines = readFileSync(index, "utf8").split("\n").filter(Boolean).reverse();
      for (const line of lines) {
        try {
          const entry = JSON.parse(line) as { id: string };
          if (Number(entry.id.slice(0, 13)) < since) continue;
          const full = JSON.parse(readFileSync(path.join(OUTBOX_DIR, `${entry.id}.json`), "utf8")) as {
            to: string[];
            html: string | null;
          };
          const message = { id: entry.id, to: full.to, html: full.html ?? "" };
          if (match(message)) return message;
        } catch {
          // A line still being written.
        }
      }
    }
    await new Promise((r) => setTimeout(r, 1000));
  }
  throw new Error(`no matching outbox message in ${OUTBOX_DIR}`);
}

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

const fresh = (file: string) =>
  existsSync(file) && statSync(file).mtimeMs > seededAt() && Date.now() - statSync(file).mtimeMs < 3_600_000;

/** Sign a seeded staff member in with a magic link from the outbox, then turn on
 *  two-step sign-in (moves and approvals need a code sign-in); save the session. */
async function signIn(browser: Browser, email: string, state: string): Promise<void> {
  if (fresh(state) && existsSync(`${state}.totp`)) return;
  const context = await browser.newContext();
  const page = await context.newPage();
  const since = Date.now() - 1000;
  await page.goto("/en/sign-in");
  await page.getByRole("textbox").first().fill(email);
  await page.keyboard.press("Enter");
  await page.waitForURL(/\/check-email/, { timeout: 30_000 });
  const mail = await newestMessage(
    (m) => m.to.some((to) => to.toLowerCase() === email) && m.html.includes("sign-in/confirm"),
    since,
  );
  await page.goto(toReachableUrl(extractLink(mail.html, "sign-in/confirm")));
  await page.getByRole("link", { name: /sign in/i }).click();
  await page.waitForURL((u) => !/sign-in|check-email/.test(u.pathname), { timeout: 30_000 });
  await page.goto("/en/settings/security");
  await page.getByRole("button", { name: /turn on/i }).click();
  await expect(page.locator("code")).toBeVisible();
  const secret = ((await page.locator("code").textContent()) ?? "").replace(/\s+/g, "");
  await page.getByLabel(/code from the app/i).fill(await totpFresh(secret));
  await page.getByRole("button", { name: /confirm and turn on/i }).click();
  await expect(page.getByText(/two-step sign-in is on/i)).toBeVisible({ timeout: 10_000 });
  await page.getByRole("button", { name: /^done$/i }).click();
  // Turning it on counts as a code sign-in; the pause keeps the next person's two-factor
  // calls under that endpoint's limit.
  await page.waitForTimeout(20_000);
  await page.waitForLoadState("load");
  await context.storageState({ path: state });
  writeFileSync(`${state}.totp`, "on");
  await context.close();
}

async function as(
  browser: Browser,
  state: string,
  options: { viewport?: { width: number; height: number }; touch?: boolean } = {},
): Promise<{ context: BrowserContext; page: Page }> {
  const context = await browser.newContext({
    storageState: state,
    viewport: options.viewport ?? { width: 1440, height: 1000 },
    ...(options.touch ? { hasTouch: true, isMobile: true } : {}),
  });
  return { context, page: await context.newPage() };
}

function loadShortlist(): Shortlist {
  expect(existsSync(shortlistFile), "the set up created the shortlist").toBeTruthy();
  return JSON.parse(readFileSync(shortlistFile, "utf8")) as Shortlist;
}

test("set up: the manager and the recruiter sign in; five candidates on the shortlist", async ({ browser, request }) => {
  test.setTimeout(300_000);
  await signIn(browser, MANAGER_EMAIL, managerState);
  await signIn(browser, RECRUITER_EMAIL, recruiterState);

  // Five synthetic applicants through the public careers API (one address each: the
  // apply limit is per address).
  const references: string[] = [];
  let ip = 20 + (Date.now() % 200);
  for (let i = 1; i <= 5; i++) {
    const reference = await applyApi(
      request,
      ORG,
      JOB,
      { full_name: `Shortlist Person ${i} ${RUN}`, email: `shortlist.${i}.${RUN}@example.test` },
      { name: "cv.pdf", mimeType: "application/pdf", buffer: Buffer.from(`%PDF-1.4\n% synthetic CV ${i} ${RUN}\n%%EOF\n`) },
      `198.51.100.${ip++ % 250}`,
    );
    references.push(reference);
  }

  // The recruiter moves all five to the shortlist from the board (one bulk move).
  const { context, page } = await as(browser, recruiterState);
  await page.goto("/en/hire");
  await page.locator(`[data-job-link="${JOB}"]`).click();
  await page.waitForURL(/\/hire\/jobs\/[0-9a-f-]{36}\/pipeline/);
  const pipeline = new URL(page.url()).pathname;
  for (const reference of references) {
    const card = page.locator("[data-card]").filter({ hasText: reference });
    await expect(card).toBeVisible({ timeout: 20_000 });
    await card.getByRole("checkbox").check();
  }
  await page.getByRole("combobox", { name: "Move to stage" }).selectOption("shortlist");
  await page.getByRole("button", { name: "Move", exact: true }).click();
  for (const reference of references) {
    await expect(page.locator("[data-card]").filter({ hasText: reference })).toHaveAttribute("data-stage", "shortlist", {
      timeout: 20_000,
    });
  }
  await context.close();
  const shortlist: Shortlist = { url: pipeline.replace(/\/pipeline$/, "/shortlist"), references };
  writeFileSync(shortlistFile, JSON.stringify(shortlist));
  logCheck({ name: "hire ux budgets: set up", status: "pass", detail: `${shortlist.url} ${references.join(" ")}` });
});

test("find a past candidate by skill: 1 search", async ({ browser }) => {
  test.setTimeout(90_000);
  const { context, page } = await as(browser, recruiterState);
  await page.goto("/en/hire");
  const box = page.getByRole("textbox", { name: "A skill, for example Kubernetes" });
  await expect(box).toBeVisible({ timeout: 20_000 });
  const budget = new Budget("find a past candidate by skill", page);
  await budget.write(box, TALENT_SKILL);
  await budget.search("Enter");
  const hits = page.locator("[data-rediscovery-hits] li");
  await expect(hits.first()).toBeVisible({ timeout: 20_000 });
  // The seeded talent pool candidate: withdrew from the payments job, agreed to future roles.
  const hit = hits.filter({ hasText: JOB_TITLE });
  await expect(hit.first()).toBeVisible();
  budget.done({ searches: 1, clicks: 0, keys: 1 }, "1 search");
  await context.close();
});

test("hiring manager reviews a shortlist of 5 and records decisions: under 5 minutes", async ({ browser }) => {
  test.setTimeout(420_000);
  const shortlist = loadShortlist();
  const { context, page } = await as(browser, managerState);
  const budget = new Budget("hiring manager reviews a shortlist of 5", page);
  await page.goto(shortlist.url);
  await expect(page.locator("[data-shortlist-review]")).toBeVisible({ timeout: 30_000 });
  const articles = page.locator("[data-shortlist-candidate]");
  await expect(articles).toHaveCount(5);
  await expect(page.locator("[data-shortlist-progress]")).toHaveText("0 of 5 decided");

  // Advance two, hold one, reject two: J to the next candidate, A, H or R, then Enter
  // (advance) or Ctrl+Enter from the reason.
  const plan: { decision: "a" | "h" | "r"; reason?: string }[] = [
    { decision: "a" },
    { decision: "a" },
    { decision: "h", reason: "Waiting for the budget sign-off on a second hire." },
    { decision: "r", reason: "No production payments work shown on the CV." },
    { decision: "r", reason: "The role needs on-site work in Abu Dhabi." },
  ];
  for (const [index, step] of plan.entries()) {
    await budget.press("j");
    const article = articles.nth(index);
    await expect(article).toBeFocused();
    await budget.press(step.decision);
    if (step.reason) {
      const reason = article.locator("[data-reason]");
      await expect(reason).toBeFocused();
      await budget.write(reason, step.reason);
      await budget.press("Control+Enter");
    } else {
      await expect(article.locator("[data-record]")).toBeFocused();
      await budget.press("Enter");
    }
    const outcome = { a: "advance", h: "hold", r: "reject" }[step.decision];
    await expect(article.locator(`[data-outcome="${outcome}"]`)).toBeVisible({ timeout: 30_000 });
  }
  await expect(page.locator("[data-shortlist-progress]")).toHaveText("5 of 5 decided");
  budget.done({ ms: FIVE_MINUTES }, "under 5 minutes");
  await context.close();
});

test("approve a rejection message from mobile: 2 taps", async ({ browser }) => {
  test.setTimeout(150_000);
  // The recruiter approves (the manager submitted the rejections, so may not). Signed in
  // with a code before counting, on a 375 px touch screen.
  const { context, page } = await as(browser, recruiterState, { viewport: PHONE, touch: true });
  const rows = page.locator("[data-approval-row]").filter({ hasText: /Rejection/ });
  await expect(async () => {
    await page.goto("/en/inbox");
    await expect(rows.first()).toBeVisible({ timeout: 3_000 });
  }).toPass({ timeout: 90_000, intervals: [3_000] });
  const id = await rows.first().getAttribute("data-approval-row");
  expect(id).toBeTruthy();
  // The approver opens the item from the notification (its link is the inbox item), so
  // the taps counted are the ones in Pairly: approve, then confirm.
  await page.goto(`/en/inbox?item=${id}`);
  const approve = page.getByRole("button", { name: /^Approve/ });
  await expect(approve).toBeVisible({ timeout: 20_000 });
  const budget = new Budget("approve a rejection from mobile", page);
  await budget.tap(approve);
  const dialog = page.getByRole("dialog");
  await expect(dialog).toBeVisible();
  await budget.tap(dialog.getByRole("button", { name: "Approve", exact: true }));
  await expect(page.getByRole("status").filter({ hasText: "Approved" }).first()).toBeVisible({ timeout: 20_000 });
  budget.done({ clicks: 2, keys: 0 }, "2 taps");
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  expect(overflow, "no horizontal scroll at 375 px").toBeLessThanOrEqual(0);
  await context.close();
});

test("the shortlist review fits a 375 px phone", async ({ browser }) => {
  const shortlist = loadShortlist();
  const { context, page } = await as(browser, managerState, { viewport: PHONE, touch: true });
  await page.goto(shortlist.url);
  await expect(page.locator("[data-shortlist-candidate]").first()).toBeVisible({ timeout: 30_000 });
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  expect(overflow, "no horizontal scroll at 375 px").toBeLessThanOrEqual(0);
  await page.screenshot({ path: path.join(SCRATCH_ROOT, "shortlist-375.png"), fullPage: false });
  logCheck({ name: "hire ux budgets: shortlist at 375 px", status: "pass" });
  await context.close();
});
