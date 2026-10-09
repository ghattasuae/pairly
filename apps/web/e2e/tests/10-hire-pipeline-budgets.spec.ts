// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology

import { execFileSync } from "node:child_process";
import { mkdirSync, writeFileSync } from "node:fs";
import path from "node:path";

import {
  expect,
  test,
  type APIRequestContext,
  type Browser,
  type BrowserContext,
  type Locator,
  type Page,
} from "@playwright/test";

import { clearMailbox, extractLink, toReachableUrl, waitForMessageTo } from "../utils/mail";
import { AUTH_STATE_DIR, logCheck, SCRATCH_ROOT } from "../utils/report";
import { makeArgs, REPO_ROOT } from "../utils/stack";
import { totpFresh } from "../utils/totp";

// Click budgets of the Hire pipeline (brief 5.3, UX acceptance targets), on the seed
// data, against the running native stack (`make local-up`; PAIRLY_WEB_PORT and
// PAIRLY_API_PORT name its ports):
//
// | Task                                           | Budget            |
// | Move a candidate to the next stage             | 1 click or 1 key  |
// | Add a note to a candidate                      | 2 clicks          |
// | Send a templated message (non-sensitive)       | 3 clicks          |
// | Draft a rejection into the approval queue      | 1 key + 1 click   |
//
// Each task starts on the job's pipeline (the board), counts every click and every
// command key from there, and fails over budget. Typing the text of a note is content,
// not a command: it is reported separately. Results: <scratch>/hire-budgets.json.

test.describe.configure({ mode: "serial" });

const RUN = Date.now().toString(36);
const SLUG = `hb-${RUN}`;
const ADMIN_EMAIL = `admin+${RUN}@test.example`;
const JOB_SLUG = "senior-backend-engineer";
const API = (process.env.PAIRLY_E2E_API_URL ?? `http://127.0.0.1:${process.env.PAIRLY_API_PORT ?? "8000"}`).replace(
  /\/$/,
  "",
);
const OUT = path.join(SCRATCH_ROOT, "hire-budgets.json");
mkdirSync(SCRATCH_ROOT, { recursive: true });

type Who = "recruiter" | "manager";
const people: Record<Who, { email: string; invite: string }> = {
  recruiter: { email: `${SLUG}-hire-recruiter@example.test`, invite: "" },
  manager: { email: `${SLUG}-hire-manager@example.test`, invite: "" },
};
const statePath = (who: Who) => path.join(AUTH_STATE_DIR, `hire-${RUN}-${who}.json`);
const candidates = {
  ana: { name: "Ana Budget", email: `ana.${RUN}@example.test` },
  ben: { name: "Ben Budget", email: `ben.${RUN}@example.test` },
  cara: { name: "Cara Budget", email: `cara.${RUN}@example.test` },
  dina: { name: "Dina Budget", email: `dina.${RUN}@example.test` },
};
const results: Record<string, { clicks: number; keys: number; typed: number; ms: number; budget: string; pass: boolean }> =
  {};
let pipelineUrl = "";

function make(target: string, ...vars: string[]): string {
  return execFileSync("make", ["-s", target, ...vars, ...makeArgs()], { cwd: REPO_ROOT, encoding: "utf8" });
}

/** Counts every click and command key of one task. */
class Budget {
  clicks = 0;
  keys = 0;
  typed = 0;
  private readonly started = Date.now();

  constructor(
    private readonly name: string,
    private readonly page: Page,
  ) {}

  async click(target: Locator): Promise<void> {
    this.clicks += 1;
    await target.click();
  }

  async press(key: string): Promise<void> {
    this.keys += 1;
    await this.page.keyboard.press(key);
  }

  /** Text the person writes (content, not a command). */
  async write(target: Locator, text: string): Promise<void> {
    this.typed += text.length;
    await target.fill(text);
  }

  done(budget: { clicks: number; keys: number }, label: string): void {
    const pass = this.clicks <= budget.clicks && this.keys <= budget.keys;
    results[this.name] = { clicks: this.clicks, keys: this.keys, typed: this.typed, ms: Date.now() - this.started, budget: label, pass };
    writeFileSync(OUT, JSON.stringify(results, null, 2));
    logCheck({
      name: `hire budget: ${this.name}`,
      status: pass ? "pass" : "fail",
      detail: `${this.clicks} clicks, ${this.keys} keys (budget ${label}), ${Date.now() - this.started} ms`,
    });
    expect(this.clicks, `${this.name}: clicks`).toBeLessThanOrEqual(budget.clicks);
    expect(this.keys, `${this.name}: keys`).toBeLessThanOrEqual(budget.keys);
  }
}

/** Accept the invitation over a magic link and turn on two-step sign-in. */
async function onboard(browser: Browser, who: Who): Promise<void> {
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
  await page.waitForURL((u) => !u.pathname.includes("/invite"), { timeout: 30_000 });
  // Moving candidates and approving messages need a code sign-in.
  await page.goto("/en/settings/security");
  await page.getByRole("button", { name: /turn on/i }).click();
  await expect(page.locator("code")).toBeVisible();
  const secret = ((await page.locator("code").textContent()) ?? "").replace(/\s+/g, "");
  await page.getByLabel(/code from the app/i).fill(await totpFresh(secret));
  await page.getByRole("button", { name: /confirm and turn on/i }).click();
  await expect(page.getByText(/two-step sign-in is on/i)).toBeVisible({ timeout: 10_000 });
  await page.getByRole("button", { name: /^done$/i }).click();
  // Turning it on counts as a code sign-in for this session; the pause keeps the next
  // person's two-factor calls under that endpoint's limit.
  await page.waitForTimeout(20_000);
  await page.waitForLoadState("load");
  await context.storageState({ path: statePath(who) });
  await context.close();
}

async function as(browser: Browser, who: Who): Promise<{ context: BrowserContext; page: Page }> {
  const context = await browser.newContext({ storageState: statePath(who), viewport: { width: 1440, height: 1000 } });
  return { context, page: await context.newPage() };
}

/** Apply through the public API, the way the careers page does (dummy Turnstile token). */
async function apply(request: APIRequestContext, who: { name: string; email: string }, ip: string): Promise<void> {
  const base = `${API}/v1/hire/public/careers/${SLUG}`;
  const headers = { "X-Pairly-Client-IP": ip };
  const detail = await request.get(`${base}/jobs/${JOB_SLUG}`, { headers });
  expect(detail.ok(), await detail.text()).toBeTruthy();
  const job = (await detail.json()) as {
    notice: { en: { version: string } };
    form: { knockout_questions: { id: string }[] };
  };
  const started = await request.post(`${base}/jobs/${JOB_SLUG}/applications`, {
    headers,
    data: {
      consent: { notice_version: job.notice.en.version, accepted: true },
      contact: { full_name: who.name, email: who.email, phone: null, location: "Dubai", profile_url: null, locale: "en" },
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
      file: {
        name: "cv.pdf",
        mimeType: "application/pdf",
        buffer: Buffer.from(`%PDF-1.4\n% synthetic CV ${who.name} ${RUN}\n%%EOF\n`),
      },
    },
  });
  expect(uploaded.status(), await uploaded.text()).toBe(201);
  const submitted = await request.post(`${base}/applications/submit`, { headers, data: { token } });
  expect(submitted.status(), await submitted.text()).toBe(202);
}

const card = (page: Page, name: string) => page.locator("[data-card]").filter({ hasText: name });

async function openBoard(page: Page, query = ""): Promise<void> {
  await page.goto(`${pipelineUrl}${query}`);
  await expect(page.locator("[data-board]")).toBeVisible();
  await expect(card(page, candidates.ana.name)).toBeVisible({ timeout: 20_000 });
}

test("set up: organisation, Hire seed, recruiter and manager, four applicants", async ({ browser, request }) => {
  test.setTimeout(300_000);
  const boot = make("bootstrap-org", `NAME=Hire Budgets ${RUN}`, `SLUG=${SLUG}`, `ADMIN_EMAIL=${ADMIN_EMAIL}`);
  expect(boot).toMatch(/invite#token=/);
  const seed = make("demo-hire", `SLUG=${SLUG}`);
  expect(seed).toMatch(/recorded message drafts: \d+ imported/);
  for (const who of ["recruiter", "manager"] as const) {
    const block = seed.slice(seed.indexOf(people[who].email));
    people[who].invite = block.match(/https?:\/\/\S+\/invite#token=\S+/)?.[0] ?? "";
    expect(people[who].invite, seed).not.toBe("");
  }
  // The manager first: a rejection needs someone other than the recruiter to approve it.
  await onboard(browser, "manager");
  await onboard(browser, "recruiter");
  let ip = 10 + (Date.now() % 200);
  for (const who of Object.values(candidates)) await apply(request, who, `198.51.100.${ip++ % 250}`);

  const { context, page } = await as(browser, "recruiter");
  await page.goto("/en/hire");
  await page.locator(`[data-job-link="${JOB_SLUG}"]`).click();
  await page.waitForURL(/\/hire\/jobs\/[0-9a-f-]{36}\/pipeline/);
  pipelineUrl = new URL(page.url()).pathname;
  for (const who of Object.values(candidates)) await expect(card(page, who.name)).toBeVisible();
  logCheck({ name: "hire budgets: set up", status: "pass", detail: `${SLUG} ${pipelineUrl}` });
  await context.close();
});

test("move a candidate to the next stage: 1 click", async ({ browser }) => {
  const { context, page } = await as(browser, "recruiter");
  await openBoard(page);
  const ana = card(page, candidates.ana.name);
  await expect(ana).toHaveAttribute("data-stage", "applied");
  const budget = new Budget("move to next stage (click)", page);
  await budget.click(ana.locator("[data-advance]"));
  await expect(card(page, candidates.ana.name)).toHaveAttribute("data-stage", "screening", { timeout: 10_000 });
  await expect(page.getByRole("button", { name: "Undo" })).toBeVisible();
  budget.done({ clicks: 1, keys: 0 }, "1 click");
  // The move is real: a reload shows it.
  await openBoard(page);
  await expect(card(page, candidates.ana.name)).toHaveAttribute("data-stage", "screening");
  await context.close();
});

test("move a candidate to the next stage: 1 key", async ({ browser }) => {
  const { context, page } = await as(browser, "recruiter");
  await openBoard(page);
  // The board opens with the first candidate selected (Ana, who applied first).
  const budget = new Budget("move to next stage (key)", page);
  await budget.press("a");
  await expect(card(page, candidates.ana.name)).toHaveAttribute("data-stage", "shortlist", { timeout: 10_000 });
  budget.done({ clicks: 0, keys: 1 }, "1 key");
  await context.close();
});

test("add a note to a candidate: 2 clicks", async ({ browser }) => {
  const { context, page } = await as(browser, "recruiter");
  await openBoard(page);
  const budget = new Budget("add a note", page);
  await budget.click(card(page, candidates.ben.name).getByText(candidates.ben.name));
  const box = page.getByRole("textbox", { name: "Note" });
  await expect(box).toBeVisible({ timeout: 15_000 });
  await budget.write(box, "Clear answers on the knockout questions. Worth a call.");
  await budget.click(page.getByRole("button", { name: "Add note" }));
  await expect(page.getByText("Note added")).toBeVisible();
  await expect(page.locator('[data-event="note"]').first()).toContainText("Worth a call.");
  budget.done({ clicks: 2, keys: 0 }, "2 clicks");
  await context.close();
});

test("send a templated message (non-sensitive): 3 clicks", async ({ browser }) => {
  test.setTimeout(150_000);
  const { context, page } = await as(browser, "recruiter");
  await openBoard(page);
  await clearMailbox();
  const budget = new Budget("send a templated message", page);
  await budget.click(card(page, candidates.cara.name).getByText(candidates.cara.name));
  await budget.click(page.locator('[data-template="hire.candidate.status_update"]'));
  const preview = page.locator("[data-preview]");
  await expect(preview).toContainText(`Hello ${candidates.cara.name},`, { timeout: 15_000 });
  await budget.click(page.getByRole("button", { name: "Send", exact: true }));
  await expect(page.getByText("Message sent")).toBeVisible();
  budget.done({ clicks: 3, keys: 0 }, "3 clicks");
  // The worker delivers it to the candidate (development outbox).
  const mail = await waitForMessageTo(candidates.cara.email, 120_000);
  expect(mail.Text).toContain(`Hello ${candidates.cara.name},`);
  expect(mail.Text).not.toContain("—");
  await context.close();
});

test("draft a rejection into the approval queue: 1 key + 1 click", async ({ browser }) => {
  test.setTimeout(150_000);
  const { context, page } = await as(browser, "recruiter");
  await openBoard(page);
  const dinaId = await card(page, candidates.dina.name).getAttribute("data-card");
  expect(dinaId).toBeTruthy();
  // The candidate is open beside the board (the place a recruiter decides from).
  await openBoard(page, `?candidate=${dinaId}`);
  await expect(page.locator(`[data-candidate-panel="${dinaId}"]`)).toBeVisible({ timeout: 15_000 });
  const budget = new Budget("draft a rejection into the approval queue", page);
  await budget.press("r");
  const submit = page.getByRole("button", { name: "Submit for approval" });
  await expect(submit).toBeVisible({ timeout: 15_000 });
  await expect(page.locator("[data-compose]")).toContainText("Needs approval");
  await budget.click(submit);
  await expect(page.getByText("In the approval queue")).toBeVisible();
  budget.done({ clicks: 1, keys: 1 }, "1 key + 1 click");

  // Nothing was sent: the rejection waits for another person's approval.
  await expect(async () => {
    await openBoard(page, `?candidate=${dinaId}`);
    await expect(page.locator("[data-open-message]")).toContainText("Waiting for approval", { timeout: 3_000 });
  }).toPass({ timeout: 60_000, intervals: [2_000] });
  await context.close();

  const manager = await as(browser, "manager");
  await manager.page.goto("/en/inbox");
  await expect(manager.page.locator("[data-approval-row]").filter({ hasText: "Rejection after review" })).toBeVisible({
    timeout: 20_000,
  });
  await manager.context.close();
});
