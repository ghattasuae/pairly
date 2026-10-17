// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology

import { execFileSync } from "node:child_process";
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";

import { expect, test, type APIRequestContext, type Browser, type BrowserContext, type Locator, type Page } from "@playwright/test";

import { auditA11y } from "../utils/axe";
import { clearMailbox, extractLink, OUTBOX_DIR, toReachableUrl, waitForMessageTo } from "../utils/mail";
import { API_PORT } from "../utils/origin";
import { AUTH_STATE_DIR, logCheck, SCRATCH_ROOT } from "../utils/report";
import { makeArgs, REPO_ROOT } from "../utils/stack";

// Click budgets of interview scheduling (brief 5.3, UX acceptance targets), on the seed
// data, against the running native stack (`make local-up`; LOCAL_WEB_PORT and
// LOCAL_API_PORT name its ports):
//
// | Task                                        | Budget   |
// | Book an interview with an available slot    | 3 clicks |
// | Reschedule an interview                     | 2 clicks |
//
// Each task starts on the job's pipeline (the board) and counts every click (a drag
// counts as one) and every command key. Results: <scratch>/hire-scheduling-budgets.json.
// Then the candidate's public scheduling page: choose and book a time from the emailed
// link, and axe on that page in English and Arabic (no violations).

test.describe.configure({ mode: "serial" });

const RUN = Date.now().toString(36);
const SLUG = `hs-${RUN}`;
const ADMIN_EMAIL = `admin+${RUN}@test.example`;
const JOB_SLUG = "senior-backend-engineer";
const API = (process.env.PAIRLY_E2E_API_URL ?? `http://127.0.0.1:${API_PORT}`).replace(/\/$/, "");
const OUT = path.join(SCRATCH_ROOT, "hire-scheduling-budgets.json");
mkdirSync(SCRATCH_ROOT, { recursive: true });

const recruiter = { email: `${SLUG}-hire-recruiter@example.test`, invite: "" };
const statePath = path.join(AUTH_STATE_DIR, `sched-${RUN}-recruiter.json`);
type Candidate = { name: string; email: string; label: string };
const candidates: Record<"ana" | "ben", Candidate> = {
  ana: { name: "Ana Schedule", email: `ana.s.${RUN}@example.test`, label: "" },
  ben: { name: "Ben Schedule", email: `ben.s.${RUN}@example.test`, label: "" },
};
const results: Record<string, { clicks: number; keys: number; ms: number; budget: string; pass: boolean }> = {};
let pipelineUrl = "";

function make(target: string, ...vars: string[]): string {
  return execFileSync("make", ["-s", target, ...vars, ...makeArgs()], { cwd: REPO_ROOT, encoding: "utf8" });
}

class Budget {
  clicks = 0;
  keys = 0;
  private readonly started = Date.now();

  constructor(private readonly name: string) {}

  async click(target: Locator): Promise<void> {
    this.clicks += 1;
    await target.click();
  }

  /** A drag and drop is one pointer action. */
  async drag(source: Locator, target: Locator): Promise<void> {
    this.clicks += 1;
    await source.dragTo(target);
  }

  done(budget: { clicks: number; keys: number }, label: string): void {
    const pass = this.clicks <= budget.clicks && this.keys <= budget.keys;
    results[this.name] = { clicks: this.clicks, keys: this.keys, ms: Date.now() - this.started, budget: label, pass };
    writeFileSync(OUT, JSON.stringify(results, null, 2));
    logCheck({
      name: `hire scheduling budget: ${this.name}`,
      status: pass ? "pass" : "fail",
      detail: `${this.clicks} clicks, ${this.keys} keys (budget ${label}), ${Date.now() - this.started} ms`,
    });
    expect(this.clicks, `${this.name}: clicks`).toBeLessThanOrEqual(budget.clicks);
    expect(this.keys, `${this.name}: keys`).toBeLessThanOrEqual(budget.keys);
  }
}

/** The newest outbox message to `address` whose text contains `needle` (native stack). */
async function waitForText(address: string, needle: string, timeoutMs = 120_000): Promise<string> {
  const deadline = Date.now() + timeoutMs;
  const index = path.join(OUTBOX_DIR, "index.jsonl");
  while (Date.now() < deadline) {
    if (existsSync(index)) {
      const lines = readFileSync(index, "utf8").split("\n").filter(Boolean).reverse();
      for (const line of lines) {
        try {
          const entry = JSON.parse(line) as { id: string; to: string[] };
          if (!entry.to.some((to) => to.toLowerCase() === address.toLowerCase())) continue;
          const full = JSON.parse(readFileSync(path.join(OUTBOX_DIR, `${entry.id}.json`), "utf8")) as { text: string };
          if (full.text.includes(needle)) return full.text;
        } catch {
          // A line still being written.
        }
      }
    }
    await new Promise((r) => setTimeout(r, 1000));
  }
  throw new Error(`no message to ${address} containing ${JSON.stringify(needle)}`);
}

async function onboard(browser: Browser): Promise<void> {
  const context = await browser.newContext();
  const page = await context.newPage();
  await clearMailbox();
  await page.goto(toReachableUrl(recruiter.invite));
  await page.getByRole("button", { name: /email me a sign-in link/i }).click();
  await page.waitForURL(/\/check-email/);
  const mail = await waitForMessageTo(recruiter.email);
  await page.goto(toReachableUrl(extractLink(mail.HTML, "sign-in/confirm")));
  await page.getByRole("link", { name: /sign in/i }).click();
  await page.waitForURL(/\/invite/);
  await page.getByRole("button", { name: /accept and join/i }).click();
  await page.waitForURL((u) => !u.pathname.includes("/invite"), { timeout: 30_000 });
  await context.storageState({ path: statePath });
  await context.close();
}

async function asRecruiter(browser: Browser): Promise<{ context: BrowserContext; page: Page }> {
  const context = await browser.newContext({ storageState: statePath, viewport: { width: 1440, height: 1000 } });
  return { context, page: await context.newPage() };
}

async function apply(request: APIRequestContext, who: Candidate, ip: string): Promise<void> {
  const base = `${API}/v1/hire/public/careers/${SLUG}`;
  const headers = { "X-Pairly-Client-IP": ip };
  const detail = await request.get(`${base}/jobs/${JOB_SLUG}`, { headers });
  expect(detail.ok(), await detail.text()).toBeTruthy();
  const job = (await detail.json()) as { notice: { en: { version: string } }; form: { knockout_questions: { id: string }[] } };
  const started = await request.post(`${base}/jobs/${JOB_SLUG}/applications`, {
    headers,
    data: {
      consent: { notice_version: job.notice.en.version, accepted: true },
      contact: { full_name: who.name, email: who.email, phone: null, location: "London", profile_url: null, locale: "en" },
      answers: {},
      knockouts: Object.fromEntries(job.form.knockout_questions.map((k) => [k.id, "yes"])),
      self_identification: {},
      turnstile_token: "XXXX.DUMMY.TOKEN.XXXX",
    },
  });
  expect(started.status(), await started.text()).toBe(201);
  const { token } = (await started.json()) as { token: string };
  const uploaded = await request.post(`${base}/applications/files`, {
    headers,
    multipart: {
      token,
      field: "cv",
      file: { name: "cv.pdf", mimeType: "application/pdf", buffer: Buffer.from(`%PDF-1.4\n% synthetic CV ${who.name} ${RUN}\n%%EOF\n`) },
    },
  });
  expect(uploaded.status(), await uploaded.text()).toBe(201);
  const submitted = await request.post(`${base}/applications/submit`, { headers, data: { token } });
  expect(submitted.status(), await submitted.text()).toBe(202);
  who.label = `Candidate ${((await submitted.json()) as { reference: string }).reference}`;
}

const escape = (text: string) => text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
const shown = (who: Candidate) => new RegExp(`${escape(who.label)}|${escape(who.name)}`);
const card = (page: Page, who: Candidate) => page.locator("[data-card]").filter({ hasText: shown(who) });

async function openBoard(page: Page): Promise<void> {
  await page.goto(pipelineUrl);
  await expect(page.locator("[data-board]")).toBeVisible();
  await expect(card(page, candidates.ana)).toBeVisible({ timeout: 20_000 });
}

test("set up: organisation, Hire seed, recruiter, two applicants", async ({ browser, request }) => {
  test.setTimeout(240_000);
  const boot = make("bootstrap-org", `NAME=Hire Scheduling ${RUN}`, `SLUG=${SLUG}`, `ADMIN_EMAIL=${ADMIN_EMAIL}`);
  expect(boot).toMatch(/invite#token=/);
  const seed = make("demo-hire", `SLUG=${SLUG}`);
  const block = seed.slice(seed.indexOf(recruiter.email));
  recruiter.invite = block.match(/https?:\/\/\S+\/invite#token=\S+/)?.[0] ?? "";
  expect(recruiter.invite, seed).not.toBe("");
  await onboard(browser);
  let ip = 10 + (Date.now() % 200);
  for (const who of Object.values(candidates)) await apply(request, who, `198.51.100.${ip++ % 250}`);

  const { context, page } = await asRecruiter(browser);
  await page.goto("/en/hire");
  await page.locator(`[data-job-link="${JOB_SLUG}"]`).click();
  await page.waitForURL(/\/hire\/jobs\/[0-9a-f-]{36}\/pipeline/);
  pipelineUrl = new URL(page.url()).pathname;
  await expect(card(page, candidates.ana)).toBeVisible({ timeout: 20_000 });
  logCheck({ name: "hire scheduling: set up", status: "pass", detail: `${SLUG} ${pipelineUrl}` });
  await context.close();
});

test("book an interview with an available slot: 3 clicks", async ({ browser }) => {
  test.setTimeout(150_000);
  const { context, page } = await asRecruiter(browser);
  await openBoard(page);
  await clearMailbox();
  const budget = new Budget("book an interview with an available slot");
  await budget.click(card(page, candidates.ana).getByText(shown(candidates.ana)));
  await budget.click(page.locator("[data-schedule]"));
  const slot = page.locator("[data-suggested-slot]").first();
  await expect(slot).toBeVisible({ timeout: 20_000 });
  // Every suggested time shows the candidate's local time beside it.
  await expect(slot).toContainText("for the candidate");
  await budget.click(slot);
  await expect(page.getByText("Interview booked. Invitations are on their way.")).toBeVisible({ timeout: 15_000 });
  await expect(page.locator("[data-current-interview]")).toHaveAttribute("data-status", "booked");
  budget.done({ clicks: 3, keys: 0 }, "3 clicks");
  // The board shows the interview on the card, and the candidate gets the invitation.
  await expect(card(page, candidates.ana).locator("[data-interview]")).toBeVisible({ timeout: 10_000 });
  const text = await waitForText(candidates.ana.email, "Your interview for");
  expect(text).toContain("is booked");
  expect(text).not.toContain("\u2014"); // no em dash
  await context.close();
});

test("reschedule an interview: 2 clicks", async ({ browser }) => {
  test.setTimeout(150_000);
  const { context, page } = await asRecruiter(browser);
  await openBoard(page);
  const chip = card(page, candidates.ana).locator("[data-interview]");
  await expect(chip).toBeVisible({ timeout: 15_000 });
  const before = await chip.textContent();
  const budget = new Budget("reschedule an interview (suggested time)");
  await budget.click(chip);
  const slot = page.locator("[data-suggested-slot]").nth(1);
  await expect(slot).toBeVisible({ timeout: 20_000 });
  await budget.click(slot);
  await expect(page.getByText("Interview moved. The candidate and the panel are told.")).toBeVisible({ timeout: 15_000 });
  budget.done({ clicks: 2, keys: 0 }, "2 clicks");
  await expect(page.locator("[data-current-interview]")).toHaveAttribute("data-status", "rescheduled");
  await expect(card(page, candidates.ana).locator("[data-interview]")).not.toHaveText(before ?? "", { timeout: 10_000 });
  await context.close();
});

test("reschedule an interview by dragging it: 2 actions", async ({ browser }) => {
  test.setTimeout(150_000);
  const { context, page } = await asRecruiter(browser);
  await openBoard(page);
  const budget = new Budget("reschedule an interview (drag)");
  await budget.click(card(page, candidates.ana).locator("[data-interview]"));
  const block = page.locator("[data-interview-block]");
  await expect(block).toBeVisible({ timeout: 20_000 });
  const current = await page.locator("[data-current-interview]").getAttribute("data-current-interview");
  expect(current).toBeTruthy();
  // A free cell of the panel calendar, away from the interview itself.
  const target = page.locator("[data-free-cell]").last();
  await expect(target).toBeVisible();
  await budget.drag(block, target);
  await expect(page.getByText("Interview moved. The candidate and the panel are told.")).toBeVisible({ timeout: 15_000 });
  budget.done({ clicks: 2, keys: 0 }, "2 clicks");
  await context.close();
});

test("the candidate books from the emailed link; axe on the page in en and ar", async ({ browser }) => {
  test.setTimeout(180_000);
  const { context, page } = await asRecruiter(browser);
  await openBoard(page);
  await clearMailbox();
  await card(page, candidates.ben).getByText(shown(candidates.ben)).click();
  await page.locator("[data-schedule]").click();
  await page.locator("[data-send-link]").click();
  await expect(page.getByText("Link sent to the candidate.")).toBeVisible({ timeout: 15_000 });
  await context.close();

  const text = await waitForText(candidates.ben.email, "/schedule#hs1.");
  const link = text.match(/https?:\/\/\S+\/en\/schedule#hs1\.\S+/)?.[0];
  expect(link).toBeTruthy();
  const visitor = await browser.newContext({ timezoneId: "Europe/London" });
  const candidate = await visitor.newPage();
  await candidate.goto(toReachableUrl(link ?? ""));
  await expect(candidate.getByRole("heading", { name: "Choose your interview time" })).toBeVisible({ timeout: 20_000 });
  // The token left the address bar.
  expect(new URL(candidate.url()).hash).toBe("");
  await expect(candidate.locator("select")).toHaveValue("Europe/London");
  expect(await auditA11y(candidate, "schedule", "en", "light")).toBe(0);

  const arabic = await visitor.newPage();
  await arabic.goto(toReachableUrl((link ?? "").replace("/en/schedule", "/ar/schedule")));
  await expect(arabic.getByRole("heading", { name: "اختر موعد مقابلتك" })).toBeVisible({ timeout: 20_000 });
  await expect(arabic.locator("html")).toHaveAttribute("dir", "rtl");
  expect(await auditA11y(arabic, "schedule", "ar", "light")).toBe(0);
  await arabic.close();

  await candidate.locator("[data-candidate-slot]").first().click();
  await candidate.locator("[data-confirm-slot]").click();
  await expect(candidate.getByRole("heading", { name: "Your interview is booked" })).toBeVisible({ timeout: 20_000 });
  expect(await auditA11y(candidate, "schedule-booked", "en", "light")).toBe(0);
  const confirmed = await waitForText(candidates.ben.email, "is booked");
  expect(confirmed).toContain("(Europe/London)");
  await visitor.close();
});
