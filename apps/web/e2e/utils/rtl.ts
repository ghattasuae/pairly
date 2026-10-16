// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology

import type { Page } from "@playwright/test";

import { logRtl } from "./report";

/**
 * RTL checks for an `ar` page: `dir="rtl"` on `<html>`, the sidebar sits on the visual
 * right, and nothing overflows horizontally at 375px and 1280px.
 */
export async function auditRtl(page: Page, pageName: string): Promise<void> {
  const htmlDir = await page.locator("html").getAttribute("dir");

  // The desktop sidebar (`<aside>` right before the main column) only renders at lg+.
  await page.setViewportSize({ width: 1280, height: 900 });
  const sidebarOnRight = await page.evaluate(() => {
    const aside = document.querySelector("aside");
    if (!aside) return null;
    const rect = aside.getBoundingClientRect();
    return rect.right > window.innerWidth / 2;
  });
  const overflow1280 = await page.evaluate(
    () => document.documentElement.scrollWidth > document.documentElement.clientWidth + 1,
  );

  await page.setViewportSize({ width: 375, height: 800 });
  const overflow375 = await page.evaluate(
    () => document.documentElement.scrollWidth > document.documentElement.clientWidth + 1,
  );

  logRtl({ page: pageName, htmlDir, sidebarOnRight, overflow375, overflow1280 });
  await page.setViewportSize({ width: 1280, height: 900 });
}
