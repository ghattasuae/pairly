// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology

import { execFileSync } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { test, expect, type Page } from "@playwright/test";

import { auditA11y } from "../utils/axe";
import { auditCopy } from "../utils/copy";
import { auditRtl } from "../utils/rtl";
import { clearMailbox, extractLink, toReachableUrl, waitForMessageTo } from "../utils/mailpit";
import { logCheck, SCREENSHOTS_DIR } from "../utils/report";

// 01-full-flow.spec.ts only drives the invite "signIn"/"accept" states in en/light (the
// token is single-use, so those exact screenshots cannot be replayed for the other locale
// and theme combinations). This spec runs one more short bootstrap+invite just to get the
// `ar` and dark-theme renders of those same two states, with the same audits as 02-pages.
//
// Order matters: both theme screenshots of the (non-destructive) signIn state are taken
// first, *then* the token is spent once to reach the accept state (light + dark, same
// accepted page, just a theme toggle).

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(__dirname, "../../../..");
const RUN_ID = `ar${Date.now().toString(36)}`;
const ORG_SLUG = `e2e-${RUN_ID}`;
const ADMIN_EMAIL = `admin+${RUN_ID}@test.example`;

function record(name: string, ok: boolean, detail?: string) {
  logCheck({ name, status: ok ? "pass" : "fail", detail });
}

async function shotAndAudit(page: Page, pageName: string, name: string, theme: "light" | "dark") {
  await page.screenshot({ path: path.join(SCREENSHOTS_DIR, `${name}.png`), fullPage: true });
  await auditA11y(page, pageName, "ar", theme);
  await auditCopy(page, pageName, "ar", theme);
  await auditRtl(page, pageName);
  record(name, true);
}

test("invite landing (ar, light + dark): signIn and accept states", async ({ browser }) => {
  const out = execFileSync(
    "make",
    ["bootstrap-org", `NAME=E2E Org ${RUN_ID}`, `SLUG=${ORG_SLUG}`, `ADMIN_EMAIL=${ADMIN_EMAIL}`],
    { cwd: REPO_ROOT, encoding: "utf8" },
  );
  const m = out.match(/https?:\/\/\S+\/invite#token=\S+/);
  expect(m, `bootstrap-org output did not contain an invite link:\n${out}`).toBeTruthy();
  const inviteLink = toReachableUrl(m![0]).replace("/en/invite", "/ar/invite");

  // 1. signIn state, both themes, non-destructive: just viewing the invite.
  for (const theme of ["light", "dark"] as const) {
    const context = await browser.newContext({ colorScheme: theme });
    const page = await context.newPage();
    await page.emulateMedia({ colorScheme: theme });
    await page.goto(inviteLink);
    await expect(page.locator("main").getByRole("button")).toBeVisible();
    await shotAndAudit(page, "invite-valid-signin", `invite-valid-signin-ar-${theme}`, theme);
    await page.close();
    await context.close();
  }

  // 2. Spend the token once: sign in via the real magic link, land on the accept state.
  const context = await browser.newContext({ colorScheme: "light" });
  const page = await context.newPage();
  await clearMailbox();
  await page.goto(inviteLink);
  await page.locator("main").getByRole("button").first().click();
  await page.waitForURL(/\/check-email/);

  const mail = await waitForMessageTo(ADMIN_EMAIL);
  const confirmHref = toReachableUrl(extractLink(mail.HTML, "sign-in/confirm")).replace(
    "/en/sign-in/confirm",
    "/ar/sign-in/confirm",
  );
  await page.goto(confirmHref);
  await page.locator("main").getByRole("link").first().click();
  await page.waitForURL(/\/invite/, { timeout: 15_000 });
  await page.waitForLoadState("load");

  await shotAndAudit(page, "invite-valid-accept", "invite-valid-accept-ar-light", "light");
  await page.emulateMedia({ colorScheme: "dark" });
  await shotAndAudit(page, "invite-valid-accept", "invite-valid-accept-ar-dark", "dark");

  await page.close();
  await context.close();
});
