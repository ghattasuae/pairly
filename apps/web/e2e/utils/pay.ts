// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology

// Shared steps of the Pay demo specs (18-pay-demo, 19-pay-ux-budgets) on a stack seeded
// by `make demo` (org "demo", scripts/demo_pay.py): the seeded Pay people, their
// sessions (a magic link from the dev outbox, then two-step sign-in, which approvals,
// call records and payment actions need), and the development inbound drop.
//
// Sessions are saved under AUTH_STATE_DIR and reused by both specs for an hour on the
// same stack: the magic-link request is rate limited per address, so the second spec
// never signs the same person in again.
import { execFileSync } from "node:child_process";
import { existsSync, readFileSync, statSync, writeFileSync } from "node:fs";
import path from "node:path";

import { expect, type Browser, type BrowserContext, type Page } from "@playwright/test";

import { extractLink, OUTBOX_DIR, toReachableUrl } from "./mail";
import { AUTH_STATE_DIR } from "./report";
import { makeArgs, REPO_ROOT } from "./stack";
import { totpFresh } from "./totp";

// From scripts/demo_pay.py (PEOPLE) and scripts/demo_seed.py (the admin).
export const PAY_PEOPLE = {
  admin: "demo-admin@example.test",
  clerk: "demo-pay-clerk@example.test",
  approver: "demo-pay-approver@example.test",
  finance: "demo-pay-finance@example.test",
} as const;
export type PayPerson = keyof typeof PAY_PEOPLE;

/** The saved session of a Pay person (shared by specs 18 and 19). */
export function payState(who: PayPerson): string {
  return path.join(AUTH_STATE_DIR, `pay-demo-${who}.json`);
}

/** The demo emails `make demo` wrote (scripts/demo_pay.py DEMO_FILES), in demo order. */
export const LOCAL_DIR = process.env.PAIRLY_LOCAL_DIR ?? path.join(REPO_ROOT, ".local");
export const DEMO_EMAILS = ["1-clean.eml", "2-near-duplicate.eml", "3-bank-change.eml"].map((f) =>
  path.join(LOCAL_DIR, "inbound-demo", f),
);

/** Deliver one .eml as if mailed to Pay's inbound address (`make inbound-drop`). */
export function inboundDrop(file: string): string {
  return execFileSync("make", ["-s", "inbound-drop", `FILE=${file}`, ...makeArgs()], {
    cwd: REPO_ROOT,
    encoding: "utf8",
    timeout: 180_000,
  });
}

interface Outboxed {
  id: string;
  to: string[];
  html: string;
}

/** The newest outbox message sent at or after `since` (ms) that passes `match`. */
async function newestMessage(match: (m: Outboxed) => boolean, since: number, timeoutMs = 30_000): Promise<Outboxed> {
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

function fresh(file: string): boolean {
  return existsSync(file) && statSync(file).mtimeMs > seededAt() && Date.now() - statSync(file).mtimeMs < 3_600_000;
}

/** Sign a seeded Pay person in with a magic link from the outbox and turn on two-step
 *  sign-in (unless it is on already); save the session. Reused for an hour. */
export async function signInPay(browser: Browser, who: PayPerson): Promise<void> {
  const file = payState(who);
  if (fresh(file) && existsSync(`${file}.totp`)) return;
  const email = PAY_PEOPLE[who];
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
  const turnOn = page.getByRole("button", { name: /turn on/i });
  if (await turnOn.isVisible().catch(() => false)) {
    await turnOn.click();
    await expect(page.locator("code")).toBeVisible();
    const secret = ((await page.locator("code").textContent()) ?? "").replace(/\s+/g, "");
    await page.getByLabel(/code from the app/i).fill(await totpFresh(secret));
    await page.getByRole("button", { name: /confirm and turn on/i }).click();
    await expect(page.getByText(/two-step sign-in is on/i)).toBeVisible({ timeout: 10_000 });
    await page.getByRole("button", { name: /^done$/i }).click();
    // The session picks up the second factor on its next refresh; the pause also keeps
    // the next person's two-factor calls under that endpoint's limit.
    await page.waitForTimeout(20_000);
    await page.waitForLoadState("load");
  }
  await context.storageState({ path: file });
  writeFileSync(`${file}.totp`, "on");
  await context.close();
}

export async function asPay(
  browser: Browser,
  who: PayPerson,
  options: { viewport?: { width: number; height: number }; touch?: boolean } = {},
): Promise<{ context: BrowserContext; page: Page }> {
  const context = await browser.newContext({
    storageState: payState(who),
    viewport: options.viewport ?? { width: 1440, height: 1000 },
    ...(options.touch ? { hasTouch: true, isMobile: true } : {}),
  });
  return { context, page: await context.newPage() };
}
