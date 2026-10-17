// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology

import fs from "node:fs";

import { expect, test } from "@playwright/test";

import { adminContext, capture, expectNoOverflow } from "../utils/capture";
import { storageStatePath } from "../utils/pages";
import { logCheck } from "../utils/report";
import { loadRunState, seedApproval, type SeededApproval } from "../utils/stack";

// The unified approval inbox (WP1.13): list, SLA badges, keyboard shortcuts, decisions
// with confirmation, reason and Idempotency-Key, the URL keeping the place, RTL, mobile,
// and the step-up prompt. Approvals are seeded with `make dev-seed-approval` for the
// organisation and admin that 01-full-flow created.

test.describe.configure({ mode: "serial" });

const RUN = Date.now().toString(36);
const TITLES = {
  overdue: `Invoice SYN-${RUN}-A from Sample Supplies LLC, AED 120`,
  reject: `Invoice SYN-${RUN}-B from Sample Supplies LLC, AED 980`,
  search: `Invoice SYN-${RUN}-C travel booking, AED 2,400`,
};
let seeded: Record<keyof typeof TITLES, SeededApproval> | null = null;

const row = (id: string) => `[data-approval-row="${id}"]`;

test("seed three approvals for the admin", async () => {
  const state = loadRunState();
  test.skip(!state, "run 01-full-flow first");
  seeded = {
    overdue: seedApproval(state!, { title: TITLES.overdue, overdue: true }),
    reject: seedApproval(state!, { title: TITLES.reject }),
    search: seedApproval(state!, { title: TITLES.search, decision: true }),
  };
  logCheck({ name: "inbox: seeded approvals", status: "pass", detail: JSON.stringify(seeded) });
});

test("inbox: list, SLA badges, J/K, live updates, approve with A and one click", async ({ browser }) => {
  test.skip(!seeded, "seeding failed");
  const { context, page } = await adminContext(browser);
  await page.goto("/en/inbox");
  await expect(page.getByRole("heading", { name: "Inbox", level: 1 })).toBeVisible();
  const overdue = page.locator(row(seeded!.overdue.approval_id));
  await expect(overdue).toBeVisible();
  await expect(overdue.getByText("Overdue")).toBeVisible();
  // The notification stream is connected through our own origin.
  await expect(page.getByText("Live updates on")).toBeAttached({ timeout: 15_000 });
  await capture(page, "inbox-en-light");

  await overdue.click();
  await expect(page).toHaveURL(new RegExp(`item=${seeded!.overdue.approval_id}`));
  await expect(page.getByRole("button", { name: /^Approve/ })).toBeVisible();

  // J and K move the selection; the URL follows.
  // (The overdue item is the oldest, so it is last in the list: K moves up, J back down.)
  await page.keyboard.press("k");
  await expect(overdue).not.toHaveAttribute("aria-current", "true");
  await page.keyboard.press("j");
  await expect(overdue).toHaveAttribute("aria-current", "true");
  await expect(page.getByRole("button", { name: /^Approve/ })).toBeVisible();

  // Approve: 1 key + 1 click (brief 5.3 budget for an approval with context visible).
  let keys = 0;
  let clicks = 0;
  await page.keyboard.press("a");
  keys++;
  const dialog = page.getByRole("dialog");
  await expect(dialog).toContainText(TITLES.overdue);
  await capture(page, "inbox-approve-dialog-en-light");
  await dialog.getByRole("button", { name: "Approve", exact: true }).click();
  clicks++;
  await expect(page.getByRole("status").filter({ hasText: "Approved" }).first()).toBeVisible({ timeout: 15_000 });
  logCheck({ name: `inbox approve budget: ${keys} key + ${clicks} click`, status: keys <= 1 && clicks <= 1 ? "pass" : "fail" });

  // The decision is stored: it shows under the approved filter after a reload.
  await page.goto("/en/inbox?status=approved");
  await expect(page.locator(row(seeded!.overdue.approval_id))).toBeVisible();
  await context.close();
});

test("inbox: reject needs a written reason", async ({ browser }) => {
  test.skip(!seeded, "seeding failed");
  const { context, page } = await adminContext(browser);
  await page.goto(`/en/inbox?item=${seeded!.reject.approval_id}`);
  await expect(page.getByRole("heading", { level: 2, name: TITLES.reject })).toBeVisible();
  await page.locator(row(seeded!.reject.approval_id)).focus();
  await page.keyboard.press("r");
  const dialog = page.getByRole("dialog");
  const confirm = dialog.getByRole("button", { name: "Reject", exact: true });
  await expect(confirm).toBeDisabled();
  await dialog.getByLabel("Reason").fill("The amount does not match the purchase order.");
  await expect(confirm).toBeEnabled();
  await capture(page, "inbox-reject-dialog-en-light");
  await confirm.click();
  await expect(page.getByRole("status").filter({ hasText: "Rejected" }).first()).toBeVisible({ timeout: 15_000 });
  await page.goto(`/en/inbox?status=rejected&item=${seeded!.reject.approval_id}`);
  await expect(page.getByText("The amount does not match the purchase order.")).toBeVisible();
  await context.close();
});

test("inbox: / searches, ? shows the shortcuts", async ({ browser }) => {
  test.skip(!seeded, "seeding failed");
  const { context, page } = await adminContext(browser);
  await page.goto("/en/inbox");
  await expect(page.locator(row(seeded!.search.approval_id))).toBeVisible();
  await page.locator("body").click({ position: { x: 5, y: 300 } });
  await page.keyboard.press("/");
  await expect(page.getByRole("searchbox", { name: "Search this list" })).toBeFocused();
  await page.keyboard.type(`${RUN}-C`);
  await expect(page.locator(row(seeded!.search.approval_id))).toBeVisible();
  await expect(page.locator("[data-approval-row]")).toHaveCount(1);
  await expect(page).toHaveURL(/q=/);
  await page.keyboard.press("Escape");
  await page.keyboard.press("?");
  await expect(page.getByRole("dialog", { name: "Keyboard shortcuts" })).toBeVisible();
  await capture(page, "inbox-shortcuts-en-light");
  await page.keyboard.press("Escape");
  await context.close();
});

for (const [locale, theme] of [
  ["ar", "dark"],
  ["ar", "light"],
  ["en", "dark"],
] as const) {
  test(`inbox: ${locale} ${theme}`, async ({ browser }) => {
    test.skip(!seeded, "seeding failed");
    const { context, page } = await adminContext(browser, { theme });
    await page.goto(`/${locale}/inbox?item=${seeded!.search.approval_id}`);
    await expect(page.locator(row(seeded!.search.approval_id))).toBeVisible();
    if (locale === "ar") await expect(page.locator("html")).toHaveAttribute("dir", "rtl");
    await expectNoOverflow(page);
    await capture(page, `inbox-${locale}-${theme}`, locale, theme);
    await context.close();
  });
}

test("inbox: phone width, list then item, back keeps the list", async ({ browser }) => {
  test.skip(!seeded, "seeding failed");
  const { context, page } = await adminContext(browser, { viewport: { width: 390, height: 844 } });
  await page.goto("/en/inbox");
  const item = page.locator(row(seeded!.search.approval_id));
  await expect(item).toBeVisible();
  await expectNoOverflow(page);
  await capture(page, "inbox-mobile-list-en-light");
  await item.click();
  const back = page.getByRole("button", { name: "Back to the list" });
  await expect(back).toBeVisible();
  await expect(page.getByRole("button", { name: /^Approve/ })).toBeVisible();
  await expectNoOverflow(page);
  await capture(page, "inbox-mobile-item-en-light");
  await back.click();
  await expect(item).toBeVisible();
  await context.close();
});

test("inbox: a decider without a code sign-in is asked for two-step sign-in", async ({ browser }) => {
  const state = loadRunState();
  const file = storageStatePath("second-user.json");
  test.skip(!state || !fs.existsSync(file), "run 01-full-flow first");
  const approval = seedApproval(state!, { title: `Invoice SYN-${RUN}-D for the second user`, email: state!.memberEmail });
  const context = await browser.newContext({ storageState: file });
  const page = await context.newPage();
  await page.goto(`/en/inbox?item=${approval.approval_id}`);
  await expect(page.getByRole("link", { name: "Set up two-step sign-in" })).toBeVisible();
  await expect(page.getByRole("button", { name: /^Approve/ })).toHaveCount(0);
  await capture(page, "inbox-step-up-en-light");
  await context.close();
});
