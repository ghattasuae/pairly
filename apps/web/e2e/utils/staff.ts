// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology

// One saved session per seeded staff member of the `make demo` stack (org "demo"), shared
// by the Hire and Pay demo specs (14, 15, 17, 18 to 21). Each person signs in once with
// a magic link from the dev outbox and turns on two-step sign-in (stage moves, approvals
// and payment actions need a code sign-in); the session and the TOTP secret are saved
// under AUTH_STATE_DIR and reused for an hour on the same stack.
//
// Sharing matters twice over. Magic links are rate limited per IP (Better Auth) and per
// address (3 per 15 minutes), so each spec signing the same people in again would run
// into both. And once one spec has turned on two-step sign-in for a person, every later
// magic link sign-in of that person lands on the two-factor page, which only the saved
// secret can answer.
import { existsSync, readFileSync, statSync, writeFileSync } from "node:fs";
import path from "node:path";

import { expect, type Browser } from "@playwright/test";

import { extractLink, OUTBOX_DIR, toReachableUrl } from "./mail";
import { AUTH_STATE_DIR } from "./report";
import { totpFresh } from "./totp";

/** Sessions are reused this long (the session itself lives 3 days). */
const REUSE_MS = 3_600_000;

export interface Outboxed {
  id: string;
  to: string[];
  html: string;
}

/** The newest outbox message sent after `since` (ms) that passes `match`. */
export async function newestMessage(match: (m: Outboxed) => boolean, since: number, timeoutMs = 30_000): Promise<Outboxed> {
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

/** When this stack's outbox got its first message (the seed): older files are stale. */
export function seededAt(): number {
  const index = path.join(OUTBOX_DIR, "index.jsonl");
  const first = existsSync(index) ? readFileSync(index, "utf8").split("\n")[0] : "";
  try {
    return Number((JSON.parse(first ?? "") as { id: string }).id.slice(0, 13));
  } catch {
    return Date.now();
  }
}

/** The saved session of a seeded staff member (by email), shared by every demo spec. */
export function staffState(email: string): string {
  return path.join(AUTH_STATE_DIR, `staff-${email.toLowerCase().replace(/[^a-z0-9._-]+/g, "_")}.json`);
}

const secretFile = (state: string) => `${state}.totp`;

/** The TOTP secret this stack's run saved for the person, if any. */
function savedSecret(state: string): string | null {
  const file = secretFile(state);
  if (!existsSync(file) || statSync(file).mtimeMs <= seededAt()) return null;
  return readFileSync(file, "utf8").trim() || null;
}

function fresh(state: string): boolean {
  if (!existsSync(state) || savedSecret(state) === null) return false;
  const saved = statSync(state).mtimeMs;
  return saved > seededAt() && Date.now() - saved < REUSE_MS;
}

/**
 * Sign a seeded staff member in (magic link from the outbox, then the TOTP code when
 * two-step sign-in is on already, else turn it on) and save the session. Returns the
 * session file. A fresh saved session is reused without signing in.
 */
export async function signInStaff(browser: Browser, email: string): Promise<string> {
  const state = staffState(email);
  if (fresh(state)) return state;
  const address = email.toLowerCase();
  const context = await browser.newContext();
  const page = await context.newPage();
  const since = Date.now() - 1000;
  await page.goto("/en/sign-in");
  await page.getByRole("textbox").first().fill(email);
  await page.keyboard.press("Enter");
  await page.waitForURL(/\/check-email/, { timeout: 30_000 });
  const mail = await newestMessage(
    (m) => m.to.some((to) => to.toLowerCase() === address) && m.html.includes("sign-in/confirm"),
    since,
  );
  await page.goto(toReachableUrl(extractLink(mail.html, "sign-in/confirm")));
  await page.getByRole("link", { name: /sign in/i }).click();
  await page.waitForURL((u) => !/sign-in|check-email/.test(u.pathname), { timeout: 30_000 });

  if (new URL(page.url()).pathname.includes("/two-factor")) {
    // Two-step sign-in is on (an earlier spec turned it on): answer with its secret.
    const secret = savedSecret(state);
    if (!secret) {
      throw new Error(`${email} has two-step sign-in on but no saved secret (${secretFile(state)}): run on a fresh make demo`);
    }
    await page.getByLabel(/authentication code/i).fill(await totpFresh(secret));
    await page.getByRole("button", { name: /^verify$/i }).click();
    await page.waitForURL((u) => !u.pathname.includes("two-factor"), { timeout: 15_000 });
  } else {
    await page.goto("/en/settings/security");
    await page.getByRole("button", { name: /turn on/i }).click();
    await expect(page.locator("code")).toBeVisible();
    const secret = ((await page.locator("code").textContent()) ?? "").replace(/\s+/g, "");
    await page.getByLabel(/code from the app/i).fill(await totpFresh(secret));
    await page.getByRole("button", { name: /confirm and turn on/i }).click();
    await expect(page.getByText(/two-step sign-in is on/i)).toBeVisible({ timeout: 10_000 });
    writeFileSync(secretFile(state), secret, { mode: 0o600 });
    await page.getByRole("button", { name: /^done$/i }).click();
  }
  // The session picks up the second factor on its next refresh; the pause also keeps
  // the next person's two-factor calls under that endpoint's limit (5 a minute).
  await page.waitForTimeout(20_000);
  await page.waitForLoadState("load");
  await context.storageState({ path: state });
  await context.close();
  return state;
}
