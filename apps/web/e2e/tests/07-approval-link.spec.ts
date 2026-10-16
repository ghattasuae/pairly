// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology

import { expect, test } from "@playwright/test";

import { adminContext, capture } from "../utils/capture";
import { logCheck } from "../utils/report";
import { loadRunState, seedApproval } from "../utils/stack";

// Signed approval links (WP1.13): the page reads the token from the fragment, removes it
// from the address bar, describes the link (no state change), and decides only on an
// explicit confirm by the bound, TOTP-signed-in user. Used, expired, invalid and other
// people's links all look the same.

const RUN = Date.now().toString(36);

test("approval link: signed out, then the bound user approves, then the link is spent", async ({ browser }) => {
  const state = loadRunState();
  test.skip(!state, "run 01-full-flow first");
  const seeded = seedApproval(state!, { title: `Invoice SYN-${RUN}-L approved from an email`, link: "approve" });
  expect(seeded.link_token).toMatch(/^[A-Za-z0-9_-]{43}$/);
  const token = seeded.link_token!;

  // Signed out: the API builds `/approvals/link#<token>` without a locale.
  const anonymous = await browser.newContext();
  const anon = await anonymous.newPage();
  await anon.goto(`/approvals/link#${token}`);
  await expect(anon.getByRole("heading", { name: "Sign in to continue" })).toBeVisible();
  await expect(anon).toHaveURL(/\/en\/approvals\/link$/);
  expect(new URL(anon.url()).hash).toBe("");
  await capture(anon, "approval-link-signed-out-en-light");
  await anonymous.close();

  // The bound user, signed in with a code: see exactly what will happen, then confirm.
  const { context, page } = await adminContext(browser);
  await page.goto(`/approvals/link#${token}`);
  await expect(page.getByRole("heading", { name: "Approve this item" })).toBeVisible();
  expect(new URL(page.url()).hash).toBe("");
  await expect(page.getByText(`Invoice SYN-${RUN}-L approved from an email`)).toBeVisible();
  await capture(page, "approval-link-ready-en-light");
  await page.getByRole("button", { name: "Approve now" }).click();
  await expect(page.getByRole("heading", { name: "Done" })).toBeVisible({ timeout: 15_000 });
  await capture(page, "approval-link-done-en-light");
  logCheck({ name: "approval link: approve from link", status: "pass" });

  // Used once: the same link, opened again, now reads like any invalid one.
  await page.goto("about:blank");
  await page.goto(`/en/approvals/link#${token}`);
  await expect(page.getByRole("heading", { name: "This link cannot be used" })).toBeVisible();
  await capture(page, "approval-link-used-en-light");
  await context.close();
});

test("approval link: reject needs a reason", async ({ browser }) => {
  const state = loadRunState();
  test.skip(!state, "run 01-full-flow first");
  const seeded = seedApproval(state!, { title: `Invoice SYN-${RUN}-M rejected from Slack`, link: "reject" });
  const { context, page } = await adminContext(browser);
  await page.goto(`/en/approvals/link#${seeded.link_token}`);
  await expect(page.getByRole("heading", { name: "Reject this item" })).toBeVisible();
  const confirm = page.getByRole("button", { name: "Reject now" });
  await expect(confirm).toBeDisabled();
  await page.getByLabel("Reason (required)").fill("Duplicate of an invoice paid last week.");
  await confirm.click();
  await expect(page.getByRole("heading", { name: "Done" })).toBeVisible({ timeout: 15_000 });
  await context.close();
});

test("approval link: invalid tokens look the same in Arabic and dark", async ({ browser }) => {
  const { context, page } = await adminContext(browser, { theme: "dark" });
  await page.goto(`/ar/approvals/link#token=${"x".repeat(43)}`);
  await expect(page.locator("html")).toHaveAttribute("dir", "rtl");
  await expect(page.getByRole("heading", { name: "لا يمكن استخدام هذا الرابط" })).toBeVisible();
  await capture(page, "approval-link-invalid-ar-dark", "ar", "dark");
  await context.close();
});
