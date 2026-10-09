// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology

// Shared steps of the Hire end-to-end specs (screening mode, candidate portal): the
// organisation and its Hire seed, onboarding a hiring team member with two-step sign-in,
// and applying through the public careers API the way the careers page does.
import { execFileSync } from "node:child_process";
import path from "node:path";

import { expect, type APIRequestContext, type Browser, type BrowserContext, type Page } from "@playwright/test";

import { clearMailbox, extractLink, toReachableUrl, waitForMessageTo } from "./mail";
import { API_PORT } from "./origin";
import { AUTH_STATE_DIR } from "./report";
import { makeArgs, REPO_ROOT } from "./stack";
import { totpFresh } from "./totp";

export const API = (process.env.PAIRLY_E2E_API_URL ?? `http://127.0.0.1:${API_PORT}`).replace(/\/$/, "");

export function make(target: string, ...vars: string[]): string {
  return execFileSync("make", ["-s", target, ...vars, ...makeArgs()], { cwd: REPO_ROOT, encoding: "utf8" });
}

/** Bootstrap an organisation and seed Hire; returns the invitation links by email. */
export function seedOrg(slug: string, name: string): Map<string, string> {
  const boot = make("bootstrap-org", `NAME=${name}`, `SLUG=${slug}`, `ADMIN_EMAIL=admin+${slug}@test.example`);
  expect(boot).toMatch(/invite#token=/);
  const seed = make("demo-hire", `SLUG=${slug}`);
  const invites = new Map<string, string>();
  for (const who of ["recruiter", "manager", "interviewer"]) {
    const email = `${slug}-hire-${who}@example.test`;
    const block = seed.slice(seed.indexOf(email));
    const link = block.match(/https?:\/\/\S+\/invite#token=\S+/)?.[0];
    if (link) invites.set(email, link);
  }
  return invites;
}

export function statePath(run: string, who: string): string {
  return path.join(AUTH_STATE_DIR, `hire-${run}-${who}.json`);
}

/** Accept the invitation over a magic link and turn on two-step sign-in. */
export async function onboard(browser: Browser, invite: string, email: string, state: string): Promise<void> {
  const context = await browser.newContext();
  const page = await context.newPage();
  await clearMailbox();
  await page.goto(toReachableUrl(invite));
  await page.getByRole("button", { name: /email me a sign-in link/i }).click();
  await page.waitForURL(/\/check-email/);
  const mail = await waitForMessageTo(email);
  await page.goto(toReachableUrl(extractLink(mail.HTML, "sign-in/confirm")));
  await page.getByRole("link", { name: /sign in/i }).click();
  await page.waitForURL(/\/invite/);
  await page.getByRole("button", { name: /accept and join/i }).click();
  await page.waitForURL((u) => !u.pathname.includes("/invite"), { timeout: 30_000 });
  await page.goto("/en/settings/security");
  await page.getByRole("button", { name: /turn on/i }).click();
  await expect(page.locator("code")).toBeVisible();
  const secret = ((await page.locator("code").textContent()) ?? "").replace(/\s+/g, "");
  await page.getByLabel(/code from the app/i).fill(await totpFresh(secret));
  await page.getByRole("button", { name: /confirm and turn on/i }).click();
  await expect(page.getByText(/two-step sign-in is on/i)).toBeVisible({ timeout: 10_000 });
  await page.getByRole("button", { name: /^done$/i }).click();
  await page.waitForTimeout(20_000);
  await page.waitForLoadState("load");
  await context.storageState({ path: state });
  await context.close();
}

export async function as(
  browser: Browser,
  state: string,
  viewport = { width: 1440, height: 1000 },
): Promise<{ context: BrowserContext; page: Page }> {
  const context = await browser.newContext({ storageState: state, viewport });
  return { context, page: await context.newPage() };
}

export interface Applicant {
  full_name: string;
  email: string;
  phone?: string | null;
  location?: string | null;
  profile_url?: string | null;
}

/** Apply through the public API (dummy Turnstile token); returns the reference. */
export async function applyApi(
  request: APIRequestContext,
  org: string,
  job: string,
  who: Applicant,
  file: { name: string; mimeType: string; buffer: Buffer },
  ip: string,
  knockouts?: Record<string, string>,
): Promise<string> {
  const base = `${API}/v1/hire/public/careers/${org}`;
  const headers = { "X-Pairly-Client-IP": ip };
  const detail = await request.get(`${base}/jobs/${job}`, { headers });
  expect(detail.ok(), await detail.text()).toBeTruthy();
  const info = (await detail.json()) as { notice: { en: { version: string } }; form: { knockout_questions: { id: string }[] } };
  const started = await request.post(`${base}/jobs/${job}/applications`, {
    headers,
    data: {
      consent: { notice_version: info.notice.en.version, accepted: true },
      contact: {
        full_name: who.full_name,
        email: who.email,
        phone: who.phone ?? null,
        location: who.location ?? "Dubai",
        profile_url: who.profile_url ?? null,
        locale: "en",
      },
      answers: {},
      knockouts: knockouts ?? Object.fromEntries(info.form.knockout_questions.map((k) => [k.id, "yes"])),
      self_identification: {},
      turnstile_token: "XXXX.DUMMY.TOKEN.XXXX",
    },
  });
  expect(started.status(), await started.text()).toBe(201);
  const { token } = (await started.json()) as { token: string };
  const uploaded = await request.post(`${base}/applications/files`, {
    headers,
    multipart: { token, field: "cv", file },
  });
  expect(uploaded.status(), await uploaded.text()).toBe(201);
  const submitted = await request.post(`${base}/applications/submit`, { headers, data: { token } });
  expect(submitted.status(), await submitted.text()).toBe(202);
  return ((await submitted.json()) as { reference: string }).reference;
}
