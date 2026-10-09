// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology

import { mkdirSync, writeFileSync } from "node:fs";
import path from "node:path";

import { expect, test, type Page } from "@playwright/test";

import { auditA11y } from "../utils/axe";
import { applyApi, seedOrg } from "../utils/hire";
import { toReachableUrl, waitForMessageTo } from "../utils/mail";
import { logCheck, SCRATCH_ROOT } from "../utils/report";

// The candidate portal (WP2.9) against the running native stack (`make local-up`):
// a candidate applies on the seed data, opens the portal from the magic link in the
// acknowledgement email (no password), sees where the application stands, and
// withdraws it (confirmed, then an email confirms it). Axe checks every portal page in
// English and Arabic (light theme; the Arabic pages are right to left). Results:
// <scratch>/hire-portal.json.

test.describe.configure({ mode: "serial" });

const RUN = Date.now().toString(36);
const SLUG = `hp-${RUN}`;
const JOB = "senior-backend-engineer";
const CANDIDATE = { full_name: "Rana Portal", email: `rana.${RUN}@example.test` };
const OUT = path.join(SCRATCH_ROOT, "hire-portal.json");
mkdirSync(SCRATCH_ROOT, { recursive: true });

const results: Record<string, unknown> = {};
let portalLink = "";
let applicationPath = "";

async function a11y(page: Page, name: string, locale: "en" | "ar"): Promise<void> {
  await page.waitForLoadState("networkidle");
  if (locale === "ar") await expect(page.locator("html")).toHaveAttribute("dir", "rtl");
  const violations = await auditA11y(page, name, locale, "light");
  results[`axe ${name} ${locale}`] = violations;
  expect(violations, `${name} (${locale}) axe violations`).toBe(0);
}

test("set up: organisation, Hire seed, one application with an acknowledgement link", async ({ request }) => {
  test.setTimeout(240_000);
  seedOrg(SLUG, `Hire Portal ${RUN}`);
  const reference = await applyApi(
    request,
    SLUG,
    JOB,
    CANDIDATE,
    { name: "cv.pdf", mimeType: "application/pdf", buffer: Buffer.from(`%PDF-1.4\n% synthetic CV ${RUN}\n%%EOF\n`) },
    `198.51.100.${Date.now() % 200}`,
  );
  const mail = await waitForMessageTo(CANDIDATE.email, 120_000);
  expect(mail.Text).toContain(reference);
  expect(mail.Text).not.toContain("\u2014"); // no em dash
  portalLink = mail.Text.match(/https?:\/\/\S+\/en\/portal\/login#pl1\.\S+/)?.[0] ?? "";
  expect(portalLink, mail.Text).not.toBe("");
  results.reference = reference;
  logCheck({ name: "hire portal: set up", status: "pass", detail: `${SLUG} ${reference}` });
});

test("candidate opens the portal from the email, sees the status, and withdraws", async ({ browser }) => {
  test.setTimeout(240_000);
  const context = await browser.newContext({ viewport: { width: 390, height: 844 } });
  const page = await context.newPage();
  const started = Date.now();
  let clicks = 0;
  await page.goto(toReachableUrl(portalLink));
  // The link works once; the token leaves the address bar at once.
  await page.waitForURL(/\/en\/portal$/, { timeout: 30_000 });
  expect(page.url()).not.toContain("pl1.");
  const card = page.locator("[data-portal-application]");
  await expect(card).toHaveCount(1);
  await expect(card.locator('[aria-current="step"]')).toContainText(/Application received|Being reviewed/);
  await expect(card).not.toContainText(/score|flag|note/i);
  await a11y(page, "portal home", "en");

  clicks += 1;
  await card.locator("[data-open-application]").click();
  await page.waitForURL(/\/en\/portal\/applications\/[0-9a-f-]{36}$/);
  applicationPath = new URL(page.url()).pathname.replace(/^\/en/, "");
  await expect(page.locator("[data-portal-detail]")).toBeVisible();
  await a11y(page, "portal application", "en");

  clicks += 1;
  await page.getByRole("button", { name: "Withdraw this application" }).click();
  const dialog = page.getByRole("dialog");
  await expect(dialog).toContainText("You cannot undo this here.");
  clicks += 1;
  await dialog.getByRole("button", { name: "Yes, withdraw" }).click();
  await expect(page.locator("[data-portal-notice]")).toContainText("You withdrew this application");
  await expect(page.locator("[data-portal-detail]")).toHaveAttribute("data-status", "withdrawn");
  await expect(page.locator('[aria-current="step"]')).toContainText("Withdrawn");
  const elapsed = Date.now() - started;

  // A confirmation email, after the acknowledgement.
  await expect(async () => {
    const mail = await waitForMessageTo(CANDIDATE.email, 5_000);
    expect(mail.Text).toContain("You withdrew your application");
  }).toPass({ timeout: 120_000, intervals: [3_000] });

  await page.goto("/en/portal/privacy");
  await expect(page.locator("[data-privacy-centre]")).toBeVisible();
  await a11y(page, "portal privacy", "en");

  // The same pages in Arabic, right to left.
  for (const [name, target] of [
    ["portal home", "/ar/portal"],
    ["portal application", `/ar${applicationPath}`],
    ["portal privacy", "/ar/portal/privacy"],
  ] as const) {
    await page.goto(target);
    await a11y(page, name, "ar");
  }
  results.withdraw = { elapsed_ms: elapsed, clicks_after_link: clicks };
  logCheck({ name: "hire portal: open from email, see status, withdraw", status: "pass", detail: `${elapsed} ms, ${clicks} clicks` });
  await context.close();
});

test("the sign-in page and an expired link read well in English and Arabic", async ({ browser }) => {
  const context = await browser.newContext();
  const page = await context.newPage();
  for (const locale of ["en", "ar"] as const) {
    await page.goto(`/${locale}/portal?org=${SLUG}`);
    await expect(page.locator("[data-portal-sign-in]")).toBeVisible();
    await a11y(page, "portal sign-in", locale);
  }
  // The link from the email was used: it never works twice, and offers a new one.
  await page.goto(toReachableUrl(portalLink));
  await expect(page.getByRole("heading", { name: "This link does not work any more" })).toBeVisible({ timeout: 20_000 });
  await page.getByLabel("Email address").fill(CANDIDATE.email);
  await page.getByRole("button", { name: "Email me a link" }).click();
  await expect(page.locator("[data-link-sent]")).toBeVisible();
  await context.close();
  writeFileSync(OUT, JSON.stringify(results, null, 2));
});
