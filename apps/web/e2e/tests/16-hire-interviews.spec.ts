// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology

import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";

import { expect, test, type Page } from "@playwright/test";

import { OUTBOX_DIR, toReachableUrl } from "../utils/mail";
import { logCheck, SCRATCH_ROOT } from "../utils/report";

// Hire interviews (M3, brief 5.3 UX acceptance) on a stack seeded by `make demo` (org
// "demo", scripts/demo_seed.py books interviews with the demo interviewer, so their
// briefings are in the outbox), against the running native stack:
//
// | Task                                                         | Budget                          |
// | Interviewer opens scorecard from invite and submits          | under 3 minutes, 1 login step   |
//
// The task starts at the one-click scorecard link of the interviewer's briefing email
// (the same link is in the calendar invitation), on a phone, signed out, and ends when
// the scorecard is submitted. Login steps count every authentication the interviewer
// goes through: each magic link used (the scorecard link itself) and each visit to a
// sign-in, check-email, two-factor or invitation page. Clicks and typed characters are
// reported too. Results: <scratch>/hire-interviews.json.
//
// The links are single use: run once per `make demo`, with one worker.

test.describe.configure({ mode: "serial" });

const ORG = "demo";
const INTERVIEWER_EMAIL = `${ORG}-hire-interviewer@example.test`;
const PHONE = { width: 375, height: 812 };
const THREE_MINUTES = 3 * 60_000;
const OUT = path.join(SCRATCH_ROOT, "hire-interviews.json");
const LINK_RE = /https?:\/\/[^\s"<>]+\/scorecard#sk1\.[A-Za-z0-9._-]+/g;
const AUTH_PATH_RE = /\/(sign-in|check-email|two-factor|invite)(\/|$|\?)/;

mkdirSync(SCRATCH_ROOT, { recursive: true });

/** Every scorecard link in the interviewer's briefings, newest first. */
function briefingLinks(): string[] {
  const index = path.join(OUTBOX_DIR, "index.jsonl");
  if (!existsSync(index)) return [];
  const links: string[] = [];
  for (const line of readFileSync(index, "utf8").split("\n").filter(Boolean).reverse()) {
    try {
      const entry = JSON.parse(line) as { id: string };
      const full = JSON.parse(readFileSync(path.join(OUTBOX_DIR, `${entry.id}.json`), "utf8")) as {
        to: string[];
        text: string;
        subject?: string;
      };
      if (!full.to.some((to) => to.includes(INTERVIEWER_EMAIL))) continue;
      if (!full.text.includes("You are on the interview panel")) continue;
      for (const match of full.text.match(LINK_RE) ?? []) links.push(match);
    } catch {
      // A line still being written.
    }
  }
  return [...new Set(links)];
}

function watchLoginSteps(page: Page): { count: () => number } {
  let steps = 0;
  page.on("framenavigated", (frame) => {
    if (frame !== page.mainFrame()) return;
    if (AUTH_PATH_RE.test(new URL(frame.url()).pathname)) steps += 1;
  });
  return { count: () => steps };
}

test("interviewer opens the scorecard from the invite and submits: under 3 minutes, at most 1 login step", async ({
  browser,
}) => {
  test.setTimeout(6 * 60_000);
  const links = briefingLinks();
  expect(links.length, "the demo briefings carry scorecard links").toBeGreaterThan(0);

  for (const link of links) {
    const context = await browser.newContext({ viewport: PHONE, isMobile: true, hasTouch: true });
    const page = await context.newPage();
    const auth = watchLoginSteps(page);
    let clicks = 0;
    let typed = 0;
    const started = Date.now();

    // The one login step: the one-click link from the invitation.
    let loginSteps = 1;
    await page.goto(toReachableUrl(link));
    const form = page.locator("[data-scorecard]");
    const invalid = page.getByText(/has been used or has expired/i);
    await expect(form.or(invalid)).toBeVisible({ timeout: 30_000 });
    if ((await invalid.isVisible()) || (await form.getAttribute("data-status")) === "submitted") {
      // Seeded as submitted (the demo debrief) or used before: try the next interview.
      await context.close();
      continue;
    }
    await expect(page.locator("[data-candidate-label]")).toBeVisible();

    const criteria = page.locator("[data-criterion]");
    const count = await criteria.count();
    expect(count).toBeGreaterThanOrEqual(3);
    expect(count).toBeLessThanOrEqual(5);
    for (let i = 0; i < count; i++) {
      await criteria.nth(i).locator('[data-rating="3"]').tap();
      clicks += 1;
    }
    const note = "Clear examples of past work; asked good questions about the team.";
    await page.locator("[data-notes]").fill(note);
    typed += note.length;
    await page.locator('[data-recommendation="yes"]').tap();
    clicks += 1;
    await page.locator("[data-submit-scorecard]").tap();
    clicks += 1;
    await expect(page.locator("[data-submitted]")).toBeVisible({ timeout: 30_000 });
    const ms = Date.now() - started;
    loginSteps += auth.count();
    await context.close();

    const pass = ms < THREE_MINUTES && loginSteps <= 1;
    const result = { ms, loginSteps, clicks, typed, criteria: count, budget: "under 3 minutes, at most 1 login step", pass };
    writeFileSync(OUT, JSON.stringify({ "interviewer scorecard from invite": result }, null, 2));
    logCheck({
      name: "hire ux budget: interviewer scorecard from invite",
      status: pass ? "pass" : "fail",
      detail: `${ms} ms, ${loginSteps} login step(s), ${clicks} taps, ${typed} typed`,
    });
    expect(loginSteps, "login steps").toBeLessThanOrEqual(1);
    expect(ms, "time").toBeLessThan(THREE_MINUTES);
    return;
  }
  throw new Error("no unused scorecard link in the interviewer's briefings: run `make demo` again");
});
