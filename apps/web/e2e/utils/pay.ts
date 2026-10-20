// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology

// Shared steps of the Pay demo specs (18-pay-demo, 19-pay-ux-budgets, 20-pay-a11y,
// 21-pay-complete) on a stack seeded
// by `make demo` (org "demo", scripts/demo_pay.py): the seeded Pay people, their
// sessions (a magic link from the dev outbox, then two-step sign-in, which approvals,
// call records and payment actions need), and the development inbound drop.
//
// Sessions come from utils/staff.ts: one per person, saved under AUTH_STATE_DIR and
// reused by every demo spec for an hour on the same stack (magic links are rate
// limited per IP and per address, so later specs never sign the same person in again).
import { execFileSync } from "node:child_process";
import path from "node:path";

import type { Browser, BrowserContext, Page } from "@playwright/test";

import { toReachableUrl } from "./mail";
import { signInStaff, staffState } from "./staff";
import { LOCAL_DIR, makeArgs, REPO_ROOT } from "./stack";
import { demoViewport } from "./viewport";

export { newestMessage, type Outboxed } from "./staff";

// From scripts/demo_pay.py (PEOPLE) and scripts/demo_seed.py (the admin).
export const PAY_PEOPLE = {
  admin: "demo-admin@example.test",
  clerk: "demo-pay-clerk@example.test",
  approver: "demo-pay-approver@example.test",
  finance: "demo-pay-finance@example.test",
} as const;
export type PayPerson = keyof typeof PAY_PEOPLE;

/** The saved session of a Pay person (utils/staff.ts, shared by every demo spec). */
export function payState(who: PayPerson): string {
  return staffState(PAY_PEOPLE[who]);
}

/** The demo emails `make demo` wrote (scripts/demo_pay.py DEMO_FILES), in demo order. */
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

/** Sign a seeded Pay person in (utils/staff.ts: one session per person, shared with the
 *  Hire demo specs, two-step sign-in on); reused for an hour. */
export async function signInPay(browser: Browser, who: PayPerson): Promise<void> {
  await signInStaff(browser, PAY_PEOPLE[who]);
}

export async function asPay(
  browser: Browser,
  who: PayPerson,
  options: { viewport?: { width: number; height: number }; touch?: boolean; theme?: "light" | "dark" } = {},
): Promise<{ context: BrowserContext; page: Page }> {
  const context = await browser.newContext({
    storageState: payState(who),
    colorScheme: options.theme ?? "light",
    viewport: options.viewport ?? demoViewport(),
    ...(options.touch ? { hasTouch: true, isMobile: true } : {}),
  });
  return { context, page: await context.newPage() };
}

/** The vendor portal sign-in link of an outbox email (written as text, not a link). */
export function portalLoginLink(html: string): string {
  const found = /https?:\/\/[^\s<>"]*\/vendor-portal\/login#[A-Za-z0-9._~-]+/.exec(html);
  if (!found) throw new Error(`no vendor portal sign-in link in the email:\n${html}`);
  return toReachableUrl(found[0].replace(/&amp;/g, "&"));
}
