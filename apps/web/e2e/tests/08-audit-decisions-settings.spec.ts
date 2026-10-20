// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology

import { expect, test } from "@playwright/test";

import { adminContext, capture, expectNoOverflow } from "../utils/capture";
import { logCheck } from "../utils/report";
import { loadRunState, seedApproval } from "../utils/stack";

// Audit viewer, decision viewer, settings pages, global search and the notification
// bell (WP1.13), as the full-flow admin (TOTP session), with axe in en/ar and light/dark.

test("audit: events, chain check, filters, details, CSV export", async ({ browser }) => {
  const { context, page } = await adminContext(browser);
  await page.goto("/en/audit");
  await expect(page.getByRole("heading", { name: "Audit", level: 1 })).toBeVisible();
  const table = page.getByRole("table", { name: "Audit events" });
  await expect(table.getByRole("row").nth(1)).toBeVisible();
  await capture(page, "audit-en-light");

  await page.getByRole("button", { name: "Check the chain" }).click();
  await expect(page.getByText(/The chain is intact|runs in the background/)).toBeVisible({ timeout: 30_000 });
  logCheck({ name: "audit: chain check", status: "pass" });

  // Filter by one action, open one event.
  await page.getByRole("button", { name: /Filters/ }).click();
  await page.getByLabel("Action").fill("core.invitation.accepted");
  await page.getByRole("button", { name: "Search", exact: true }).click();
  await expect(page).toHaveURL(/action=core\.invitation\.accepted/);
  await page.getByRole("button", { name: /Show details of event/ }).first().click();
  await expect(page.getByText("Previous hash")).toBeVisible();
  await capture(page, "audit-filtered-en-light");

  const download = page.waitForEvent("download");
  await page.getByRole("button", { name: "Export CSV" }).click();
  const file = await download;
  expect(file.suggestedFilename()).toMatch(/^audit-.*\.csv$/);
  logCheck({ name: "audit: CSV export (step-up session)", status: "pass" });
  await context.close();
});

for (const [locale, theme] of [
  ["ar", "dark"],
  ["en", "dark"],
] as const) {
  test(`audit: ${locale} ${theme}`, async ({ browser }) => {
    const { context, page } = await adminContext(browser, { theme });
    await page.goto(`/${locale}/audit`);
    await expect(page.getByRole("table")).toBeVisible();
    await expectNoOverflow(page);
    await capture(page, `audit-${locale}-${theme}`, locale, theme);
    await context.close();
  });
}

test("decision viewer: receipt, explanations, PDF, replay", async ({ browser }) => {
  const state = loadRunState();
  test.skip(!state, "run 01-full-flow first");
  const seeded = seedApproval(state!, { title: "Invoice with a decision record", decision: true });
  expect(seeded.decision_id).toMatch(/^dec_/);
  const { context, page } = await adminContext(browser);
  await page.goto(`/en/decisions/${seeded.decision_id}`);
  await expect(page.getByRole("heading", { name: "Risk score", level: 1 })).toBeVisible();
  await expect(page.getByText("The amount is far from earlier ones, so it needs a closer look.")).toBeVisible();
  await expect(page.getByText(/contains text in a font too small for a person to read/)).toBeVisible();
  await capture(page, "decision-en-light");

  const download = page.waitForEvent("download");
  await page.getByRole("link", { name: "Download PDF (Arabic)" }).click();
  expect((await download).suggestedFilename()).toBe(`receipt-${seeded.decision_id}-ar.pdf`);

  const replay = page.getByRole("button", { name: "Replay now" });
  if (await replay.isVisible()) {
    await replay.click();
    await expect(page.getByText(/Same result|Different result|Replay failed/).first()).toBeVisible({ timeout: 30_000 });
  }
  await page.goto(`/ar/decisions/${seeded.decision_id}`);
  await expect(page.locator("html")).toHaveAttribute("dir", "rtl");
  await expectNoOverflow(page);
  await capture(page, "decision-ar-light", "ar", "light");

  // The approval that points at the record links to it from the inbox.
  await page.goto(`/en/inbox?item=${seeded.approval_id}`);
  await expect(page.getByRole("link", { name: seeded.decision_id! })).toBeVisible();
  await context.close();
});

const SETTINGS = [
  ["settings", "/settings", "Settings"],
  ["settings-notifications", "/settings/notifications", "Notifications"],
  ["settings-flags", "/settings/flags", "Feature flags"],
  ["settings-api-keys", "/settings/api-keys", "API keys"],
  ["settings-webhooks", "/settings/webhooks", "Webhooks"],
  ["settings-policies", "/settings/policies", "Policies"],
] as const;

for (const [name, path, heading] of SETTINGS) {
  for (const [locale, theme] of [
    ["en", "light"],
    ["ar", "dark"],
  ] as const) {
    test(`settings page: ${name} ${locale} ${theme}`, async ({ browser }) => {
      const { context, page } = await adminContext(browser, { theme });
      await page.goto(`/${locale}${path}`);
      if (locale === "en") await expect(page.getByRole("heading", { name: heading, level: 1 })).toBeVisible();
      else await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
      await expectNoOverflow(page);
      await capture(page, `${name}-${locale}-${theme}`, locale, theme);
      await context.close();
    });
  }
}

test("settings: notification preferences save", async ({ browser }) => {
  const { context, page } = await adminContext(browser);
  await page.goto("/en/settings/notifications");
  await page.getByLabel("Slack for Reminders").uncheck();
  await page.getByLabel("Delivery for Updates").selectOption("daily");
  await page.getByRole("switch", { name: "Use quiet hours" }).click();
  await page.getByRole("button", { name: "Save preferences" }).click();
  await expect(page.getByRole("status").filter({ hasText: "Preferences saved" })).toBeVisible();
  await page.reload();
  await expect(page.getByLabel("Slack for Reminders")).not.toBeChecked();
  await expect(page.getByLabel("Delivery for Updates")).toHaveValue("daily");
  await context.close();
});

test("settings: API key shown once, then revoked", async ({ browser }) => {
  const { context, page } = await adminContext(browser);
  await page.goto("/en/settings/api-keys");
  await page.getByLabel("Name").fill(`e2e key ${Date.now().toString(36)}`);
  await page.locator('input[type="checkbox"]').first().check();
  await page.getByRole("button", { name: "Create key" }).click();
  await expect(page.getByText("Your new API key")).toBeVisible({ timeout: 15_000 });
  await expect(page.getByText("You will not see it again.")).toBeVisible();
  await capture(page, "settings-api-key-created-en-light");
  await page.getByRole("button", { name: "I have saved it" }).click();
  await expect(page.getByText("Your new API key")).toHaveCount(0);
  await page.getByRole("button", { name: "Revoke" }).first().click();
  await page.getByRole("dialog").getByRole("button", { name: "Revoke" }).click();
  await expect(page.getByText("Revoked", { exact: true }).first()).toBeVisible();
  await context.close();
});

test("command palette and notification bell", async ({ browser }) => {
  const { context, page } = await adminContext(browser);
  await page.goto("/en");
  // The shortcut listens only once the shell has hydrated (app-shell.tsx sets this).
  await page.locator("[data-shell-ready]").waitFor({ state: "attached", timeout: 30_000 });
  await page.keyboard.press("ControlOrMeta+k");
  const palette = page.getByRole("dialog", { name: "Command menu" });
  await expect(palette).toBeVisible();
  await page.keyboard.type("Audit");
  await capture(page, "command-palette-en-light");
  await page.keyboard.press("Enter");
  await expect(page).toHaveURL(/\/en\/audit/);

  await page.getByRole("button", { name: /^Notifications/ }).click();
  await expect(page.getByRole("menu")).toBeVisible();
  await capture(page, "notification-bell-en-light");
  await context.close();
});
