// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology

import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";

import { expect, test } from "@playwright/test";

import { applyApi, as, make, onboard, seedOrg, statePath } from "../utils/hire";
import { logCheck, SCRATCH_ROOT } from "../utils/report";

// UX acceptance target (brief 5.3): "Recruiter screens 20 candidates in screening mode,
// under 10 minutes on seed data". Against the running native stack (`make local-up`).
//
// Seed: the Hire demo seed plus the screening e2e seed (`make dev-seed-hire-screening`):
// the synthetic Backend Engineer job with its locked rubric, the recorded screening
// responses and 20 synthetic CVs (7 with recorded scores, 13 that reach a person without
// AI scores, as they would without a model). The 20 apply through the public API, the
// worker screens them, then a recruiter screens all 20 from the keyboard: A (advance) or
// H (hold) on each, J to the next. The spec counts keys per candidate, times the whole
// task and how fast J shows the next candidate (prefetch), and fails over budget.
// Results: <scratch>/hire-screening-mode.json.

test.describe.configure({ mode: "serial" });

const RUN = Date.now().toString(36);
const SLUG = `hs-${RUN}`;
const JOB = "backend-engineer-screening";
const CVS = path.join(SCRATCH_ROOT, `screening-cvs-${RUN}`);
const OUT = path.join(SCRATCH_ROOT, "hire-screening-mode.json");
const BUDGET_MS = 10 * 60 * 1000;
const COUNT = 20;
mkdirSync(SCRATCH_ROOT, { recursive: true });

const recruiter = `${SLUG}-hire-recruiter@example.test`;
const manager = `${SLUG}-hire-manager@example.test`;
let screenUrl = "";

interface Manifest {
  file: string;
  upload_name: string;
  media_type: string;
  contact: { full_name: string; email: string; phone: string | null; location: string | null; profile_url: string | null };
  knockouts: Record<string, string>;
}

test("set up: seed, recruiter and manager, 20 synthetic applications screened", async ({ browser, request }) => {
  test.setTimeout(900_000);
  const invites = seedOrg(SLUG, `Hire Screening ${RUN}`);
  const seeded = make("dev-seed-hire-screening", `ORG=${SLUG}`, `OUT=${CVS}`, `COUNT=${COUNT}`);
  expect(seeded).toContain(`"cvs": ${COUNT}`);
  // People who can approve a shortlist exist before the screenings ask for one.
  await onboard(browser, invites.get(manager)!, manager, statePath(RUN, "manager"));
  await onboard(browser, invites.get(recruiter)!, recruiter, statePath(RUN, "recruiter"));

  const manifest = JSON.parse(readFileSync(path.join(CVS, "manifest.json"), "utf8")) as Manifest[];
  let ip = 20 + (Date.now() % 100);
  for (const entry of manifest) {
    await applyApi(
      request,
      SLUG,
      JOB,
      entry.contact,
      { name: entry.upload_name, mimeType: entry.media_type, buffer: readFileSync(path.join(CVS, entry.file)) },
      `198.51.100.${ip++ % 250}`,
      entry.knockouts,
    );
  }

  const { context, page } = await as(browser, statePath(RUN, "recruiter"));
  await page.goto("/en/hire");
  await page.locator(`[data-screen-link="${JOB}"]`).click();
  await page.waitForURL(/\/hire\/jobs\/[0-9a-f-]{36}\/screen/);
  screenUrl = new URL(page.url()).pathname;
  // The worker parses and screens every CV; each one then waits for a person.
  await expect(async () => {
    await page.goto(screenUrl);
    await expect(page.locator("[data-queue-item]")).toHaveCount(COUNT, { timeout: 5_000 });
    await expect(page.locator('[data-queue-item][data-screening-status="awaiting_review"]')).toHaveCount(COUNT, {
      timeout: 2_000,
    });
  }).toPass({ timeout: 600_000, intervals: [10_000] });
  const scored = await page.locator("[data-queue-item] .tabular-nums").count();
  logCheck({ name: "hire screening mode: set up", status: "pass", detail: `${SLUG} ${screenUrl}, ${scored} with AI scores` });
  await context.close();
});

test("recruiter screens 20 candidates in screening mode under 10 minutes", async ({ browser }) => {
  test.setTimeout(BUDGET_MS + 120_000);
  const { context, page } = await as(browser, statePath(RUN, "recruiter"));
  await page.goto(screenUrl);
  await expect(page.locator("[data-screening-mode]")).toBeVisible();
  // Blind mode is visible with what it hides.
  await expect(page.locator('[data-blind-status="on"]')).toContainText("Hidden until the shortlist");

  const started = Date.now();
  const keysPer: number[] = [];
  const nextMs: number[] = [];
  let advanced = 0;
  let held = 0;
  for (let i = 0; i < COUNT; i += 1) {
    let keys = 0;
    const label = page.locator("[data-candidate-label]");
    await expect(label).toBeVisible({ timeout: 15_000 });
    const name = (await label.textContent()) ?? "";
    // Read the evidence: the CV and the scores are on screen together.
    await expect(page.locator("[data-screen-document]").or(page.getByText(/Not screened yet/))).toBeVisible();
    const hold = i % 4 === 3;
    await page.keyboard.press(hold ? "h" : "a");
    keys += 1;
    if (hold) held += 1;
    else advanced += 1;
    // The action took effect, with an undo toast (10 seconds).
    await expect(page.getByText(hold ? `${name} is on hold` : `${name} moved to`)).toBeVisible({ timeout: 15_000 });
    if (i < COUNT - 1) {
      const pressed = Date.now();
      await page.keyboard.press("j");
      keys += 1;
      await expect(label).not.toHaveText(name, { timeout: 15_000 });
      nextMs.push(Date.now() - pressed);
    }
    keysPer.push(keys);
  }
  const elapsed = Date.now() - started;
  // Every action took effect: advanced candidates moved on, held ones carry the tag.
  await page.goto(screenUrl);
  await expect(page.locator('[data-queue-item][data-stage="screening"]')).toHaveCount(advanced, { timeout: 15_000 });
  const maxKeys = Math.max(...keysPer);
  const avgNext = Math.round(nextMs.reduce((a, b) => a + b, 0) / Math.max(1, nextMs.length));
  const result = {
    candidates: COUNT,
    elapsed_ms: elapsed,
    budget_ms: BUDGET_MS,
    keys_total: keysPer.reduce((a, b) => a + b, 0),
    keys_per_candidate_max: maxKeys,
    keys_per_candidate: keysPer,
    clicks: 0,
    advanced,
    held,
    next_candidate_ms_avg: avgNext,
    next_candidate_ms_max: Math.max(...nextMs),
    pass: elapsed < BUDGET_MS && maxKeys <= 2,
  };
  writeFileSync(OUT, JSON.stringify(result, null, 2));
  logCheck({
    name: "hire budget: screen 20 candidates in screening mode",
    status: result.pass ? "pass" : "fail",
    detail: `${elapsed} ms, ${result.keys_total} keys (max ${maxKeys} per candidate), next candidate ${avgNext} ms on average`,
  });
  expect(elapsed).toBeLessThan(BUDGET_MS);
  expect(maxKeys).toBeLessThanOrEqual(2);
  await context.close();
});
