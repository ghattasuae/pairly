// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology

import { execFileSync } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { test, expect, type Page, type BrowserContext } from "@playwright/test";

import { totpFresh } from "../utils/totp";
import { clearMailbox, waitForMessageTo, extractLink, toReachableUrl } from "../utils/mailpit";
import { AUTH_STATE_DIR, SCREENSHOTS_DIR, logCheck } from "../utils/report";
import { makeArgs, saveRunState } from "../utils/stack";
import { auditA11y } from "../utils/axe";
import { auditCopy } from "../utils/copy";

// Full M0 flow: bootstrap an org and admin invite, accept it over a real magic link read
// from Mailpit, turn on TOTP, sign out, sign back in with a TOTP code, invite a second
// user and accept as them. Runs first (file name sort) and saves storageState snapshots
// to the scratch dir for every other spec file to reuse, so the whole flow only runs once.
//
// This is also where the screenshots for the session-dependent page states live: invite
// (valid / wrongUser), home (not-provisioned / set-up-2FA / ready), Members (admin+TOTP).

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(__dirname, "../../../..");
const RUN_ID = Date.now().toString(36);
const ORG_SLUG = `e2e-${RUN_ID}`;
const ADMIN_EMAIL = `admin+${RUN_ID}@test.example`;
const MEMBER_EMAIL = `member+${RUN_ID}@test.example`;

let adminInviteLink = "";
let memberInviteLink = "";
let totpSecret = "";

test.describe.configure({ mode: "serial" });

async function shot(page: Page, name: string) {
  await page.screenshot({ path: path.join(SCREENSHOTS_DIR, `${name}.png`), fullPage: true });
}

/** Screenshot + axe + copy audit in one call, for the token-bound states this file owns. */
async function capture(page: Page, name: string, locale: "en" | "ar" = "en", theme: "light" | "dark" = "light") {
  await shot(page, name);
  await auditA11y(page, name, locale, theme);
  await auditCopy(page, name, locale, theme);
}

function record(name: string, ok: boolean, detail?: string) {
  logCheck({ name, status: ok ? "pass" : "fail", detail });
}

test.describe("Full flow: bootstrap -> accept -> TOTP -> sign in -> invite second user", () => {
  test("bootstrap org and admin invite", async () => {
    const out = execFileSync(
      "make",
      ["bootstrap-org", `NAME=E2E Org ${RUN_ID}`, `SLUG=${ORG_SLUG}`, `ADMIN_EMAIL=${ADMIN_EMAIL}`, ...makeArgs()],
      { cwd: REPO_ROOT, encoding: "utf8" },
    );
    const m = out.match(/https?:\/\/\S+\/invite#token=\S+/);
    expect(m, `bootstrap-org output did not contain an invite link:\n${out}`).toBeTruthy();
    adminInviteLink = toReachableUrl(m![0]);
    record("bootstrap-org", true, ORG_SLUG);
  });

  test("admin: open invite link while signed out -> signIn state, screenshot", async ({ browser }) => {
    const context = await browser.newContext();
    const page = await context.newPage();
    await clearMailbox();
    await page.goto(adminInviteLink);
    await expect(page.getByRole("button", { name: /email me a sign-in link/i })).toBeVisible();
    await capture(page, "invite-valid-signin-en-light");
    record("invite landing: valid (signIn state)", true);

    await page.getByRole("button", { name: /email me a sign-in link/i }).click();
    await page.waitForURL(/\/check-email/);
    await capture(page, "check-email-en-light");
    record("check-email page reached from invite", true);

    const mail = await waitForMessageTo(ADMIN_EMAIL);
    const confirmHref = toReachableUrl(extractLink(mail.HTML, "sign-in/confirm"));
    await page.goto(confirmHref);
    await expect(page.getByRole("link", { name: /sign in/i })).toBeVisible();
    await page.getByRole("link", { name: /sign in/i }).click();
    await page.waitForLoadState("load");

    // Lands back on the invite page, now signed in as the invited address: "accept" state.
    await page.waitForURL(/\/invite/);
    await expect(page.getByRole("button", { name: /accept and join/i })).toBeVisible();
    await capture(page, "invite-valid-accept-en-light");
    record("invite landing: valid (accept state, post magic-link)", true);

    // Before accepting: visit home directly. No membership yet -> not_provisioned state.
    await page.goto("/en");
    await expect(page.getByText(/not set up yet/i)).toBeVisible({ timeout: 15_000 });
    await capture(page, "home-not-provisioned-en-light");
    record("home: not_provisioned state", true);
    await context.storageState({ path: path.join(AUTH_STATE_DIR, "admin-not-provisioned.json") });

    // Back to the invite page (token still in localStorage within the hour) and accept.
    await page.goto(adminInviteLink.replace(/#.*/, ""));
    await page.goto(adminInviteLink);
    await page.getByRole("button", { name: /accept and join/i }).click();
    await page.waitForURL((u) => !u.pathname.includes("/invite"), { timeout: 15_000 });
    await page.waitForLoadState("load");

    // Admin role needs otp for approve/decide/manage/export: "set up two-factor" banner.
    await expect(page.getByText(/two-factor|two-step/i).first()).toBeVisible({ timeout: 15_000 });
    await capture(page, "home-set-up-2fa-en-light");
    record("home: set-up-two-factor state (admin, no TOTP yet)", true);
    await context.storageState({ path: path.join(AUTH_STATE_DIR, "admin-pre-totp.json") });

    await page.close();
    await context.close();
  });

  test("admin: enable TOTP, sign out, sign in again with a TOTP code", async ({ browser }) => {
    const context = await browser.newContext({
      storageState: path.join(AUTH_STATE_DIR, "admin-pre-totp.json"),
    });
    const page = await context.newPage();

    await page.goto("/en/settings/security");
    await page.getByRole("button", { name: /turn on/i }).click();
    await expect(page.locator("code")).toBeVisible();
    const rawSecret = (await page.locator("code").textContent()) ?? "";
    totpSecret = rawSecret.replace(/\s+/g, "");
    expect(totpSecret.length, `expected a base32 secret, got "${rawSecret}"`).toBeGreaterThan(8);

    const code1 = await totpFresh(totpSecret);
    await page.getByLabel(/code from the app/i).fill(code1);
    await page.getByRole("button", { name: /confirm and turn on/i }).click();
    await expect(page.getByText(/two-step sign-in is on/i)).toBeVisible({ timeout: 10_000 });
    await page.getByRole("button", { name: /^done$/i }).click();
    record("TOTP enable: scan -> confirm -> backup codes -> done", true);

    // Sign out.
    await page.goto("/en");
    await page.getByRole("button", { name: /account/i }).click();
    await page.getByRole("menuitem", { name: /sign out/i }).click();
    await page.waitForURL(/\/sign-in/);
    record("sign out after enabling TOTP", true);

    // Sign in again as the same (now-existing) Better Auth user.
    await clearMailbox();
    await page.goto("/en/sign-in");
    await capture(page, "sign-in-en-light");
    await page.getByLabel(/work email/i).fill(ADMIN_EMAIL);
    await page.getByRole("button", { name: /email me a link/i }).click();
    await page.waitForURL(/\/check-email/);

    const mail = await waitForMessageTo(ADMIN_EMAIL);
    const confirmHref = toReachableUrl(extractLink(mail.HTML, "sign-in/confirm"));
    await page.goto(confirmHref);
    await page.getByRole("link", { name: /sign in/i }).click();

    // TOTP is on: Better Auth's two-factor gate should land us on /two-factor.
    await page.waitForURL(/\/two-factor/, { timeout: 15_000 });
    await capture(page, "two-factor-en-light");
    record("two-factor page reached on second sign-in", true);

    const code2 = await totpFresh(totpSecret);
    await page.getByLabel(/authentication code/i).fill(code2);
    await page.getByRole("button", { name: /^verify$/i }).click();
    await page.waitForURL((u) => !u.pathname.includes("two-factor"), { timeout: 15_000 });
    await page.waitForLoadState("load");

    // Ready, no more "set up two-factor" banner.
    await expect(page.getByText(/set up two-factor|two-step sign-in to continue/i)).toHaveCount(0);
    await capture(page, "home-ready-admin-totp-en-light");
    record("home: ready state, TOTP-verified admin session", true);
    await context.storageState({ path: path.join(AUTH_STATE_DIR, "admin-with-totp.json") });
    // Later specs (06 and on) seed data for this organisation and admin.
    saveRunState({ orgSlug: ORG_SLUG, adminEmail: ADMIN_EMAIL, memberEmail: MEMBER_EMAIL });

    await page.close();
    await context.close();
  });

  test("admin with TOTP: Members page, invite a second user", async ({ browser }) => {
    const context: BrowserContext = await browser.newContext({
      storageState: path.join(AUTH_STATE_DIR, "admin-with-totp.json"),
    });
    const page = await context.newPage();

    await page.goto("/en/settings/members");
    await expect(page.getByRole("heading", { name: "Members" })).toBeVisible();
    await expect(page.getByRole("button", { name: /send invitation/i })).toBeVisible();
    await capture(page, "members-admin-totp-en-light");
    record("Members page reachable for admin with TOTP (no 404)", true);

    await clearMailbox();
    await page.getByLabel(/^email$/i).fill(MEMBER_EMAIL);
    await page.getByRole("button", { name: /send invitation/i }).click();
    // The rendered text wraps the address in Unicode bidi-isolate marks (lib/bidi.ts), so
    // match loosely across them rather than for a literal "sent to <email>" substring.
    const escapedEmail = MEMBER_EMAIL.replace(/[.+]/g, (c) => `\\${c}`);
    await expect(page.getByText(new RegExp(`sent to.*${escapedEmail}`, "i"))).toBeVisible({
      timeout: 10_000,
    });
    record("invite second user from Members page", true);

    const mail = await waitForMessageTo(MEMBER_EMAIL);
    memberInviteLink = toReachableUrl(extractLink(mail.HTML, "/invite#"));

    // While signed in as admin, the SAME browser opening the second user's invite link is
    // the "wrongUser" state (signed in as somebody else).
    await page.goto(memberInviteLink);
    await expect(page.getByText(/invitation is for someone else/i)).toBeVisible();
    await capture(page, "invite-wrong-email-en-light");
    record("invite landing: wrongUser (wrong-email) state", true);

    await page.close();
    await context.close();
  });

  test("second user: accept invite as themselves", async ({ browser }) => {
    const context = await browser.newContext();
    const page = await context.newPage();
    await clearMailbox();

    await page.goto(memberInviteLink);
    await page.getByRole("button", { name: /email me a sign-in link/i }).click();
    await page.waitForURL(/\/check-email/);

    const mail = await waitForMessageTo(MEMBER_EMAIL);
    const confirmHref = toReachableUrl(extractLink(mail.HTML, "sign-in/confirm"));
    await page.goto(confirmHref);
    await page.getByRole("link", { name: /sign in/i }).click();
    await page.waitForURL(/\/invite/);
    await page.getByRole("button", { name: /accept and join/i }).click();
    await page.waitForURL((u) => !u.pathname.includes("/invite"), { timeout: 15_000 });
    await page.waitForLoadState("load");
    await capture(page, "home-ready-second-user-en-light");
    record("second user: accepted invite and reached home", true);
    await context.storageState({ path: path.join(AUTH_STATE_DIR, "second-user.json") });

    await page.close();
    await context.close();
  });

  test("invite landing: invalid token", async ({ browser }) => {
    const context = await browser.newContext();
    const page = await context.newPage();
    await page.goto("/en/invite#token=not-a-real-token-00000000000000000000000");
    await expect(page.getByText(/cannot be used/i)).toBeVisible();
    await capture(page, "invite-invalid-en-light");
    record("invite landing: invalid token state", true);
    await page.close();
    await context.close();
  });
});
