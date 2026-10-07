// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology

// Screenshot, axe and copy audit in one call, for the WP1.13 specs (06 and on).
import fs from "node:fs";
import path from "node:path";

import { expect, test, type Browser, type BrowserContext, type Page } from "@playwright/test";

import { auditA11y } from "./axe";
import { auditCopy } from "./copy";
import { storageStatePath } from "./pages";
import { SCREENSHOTS_DIR } from "./report";
import { loadRunState, type RunState } from "./stack";

export type Locale = "en" | "ar";
export type Theme = "light" | "dark";

export async function capture(page: Page, name: string, locale: Locale = "en", theme: Theme = "light"): Promise<number> {
  await page.screenshot({ path: path.join(SCREENSHOTS_DIR, `wp113-${name}.png`), fullPage: true });
  await auditCopy(page, name, locale, theme);
  return auditA11y(page, name, locale, theme);
}

/** A browser context signed in as the full-flow admin (TOTP session), or a skipped test. */
export async function adminContext(
  browser: Browser,
  options: { theme?: Theme; viewport?: { width: number; height: number } } = {},
): Promise<{ context: BrowserContext; page: Page; state: RunState }> {
  const file = storageStatePath("admin-with-totp.json");
  const state = loadRunState();
  test.skip(!fs.existsSync(file) || !state, "run 01-full-flow first (admin session and run state)");
  const context = await browser.newContext({
    storageState: file,
    colorScheme: options.theme ?? "light",
    acceptDownloads: true,
    ...(options.viewport ? { viewport: options.viewport } : {}),
  });
  const page = await context.newPage();
  return { context, page, state: state! };
}

/** No horizontal scroll at this width (RTL and mobile checks). */
export async function expectNoOverflow(page: Page): Promise<void> {
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  expect(overflow).toBeLessThanOrEqual(1);
}
