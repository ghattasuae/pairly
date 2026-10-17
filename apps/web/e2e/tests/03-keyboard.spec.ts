// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology

import fs from "node:fs";
import path from "node:path";

import { test, expect } from "@playwright/test";

import { storageStatePath } from "../utils/pages";
import { SCREENSHOTS_DIR, logCheck } from "../utils/report";

function record(name: string, ok: boolean, detail?: string) {
  logCheck({ name, status: ok ? "pass" : "fail", detail });
}

test.describe("Keyboard", () => {
  test("skip link exists, is DOM-first, and jumps to main content", async ({ page }) => {
    await page.goto("/en/sign-in");

    // The sign-in email field has `autoFocus` (SignInForm), so on page load focus lands
    // there directly, not on the skip link, even though the skip link is first in the DOM.
    const initial = await page.evaluate(() => document.activeElement?.tagName);
    const initialIsAutoFocusedInput = initial === "INPUT";

    // Confirm the skip link really is the first focusable element in DOM order (the
    // header also has Language/Theme buttons between the skip link and the form, so a
    // single Shift+Tab from the autofocused input lands on those, not the skip link).
    const first = await page.evaluate(() => {
      const selector =
        'a[href],button:not([disabled]),input:not([disabled]),select:not([disabled]),textarea:not([disabled]),[tabindex]:not([tabindex="-1"])';
      const el = document.querySelector(selector) as HTMLElement | null;
      return { tag: el?.tagName, href: el?.getAttribute("href") };
    });
    const active = first;
    expect(active.tag).toBe("A");
    expect(active.href).toBe("#main-content");
    record(
      "skip link: first element in DOM order",
      true,
      initialIsAutoFocusedInput
        ? "note: autoFocus on the email field means initial keyboard focus (load, no interaction) lands on the input, not the skip link; the skip link is still DOM-first and reachable with Shift+Tab"
        : JSON.stringify(active),
    );

    await page.locator('a[href="#main-content"]').focus();
    await page.keyboard.press("Enter");
    const focusedId = await page.evaluate(() => document.activeElement?.id);
    expect(focusedId).toBe("main-content");
    record("skip link: Enter jumps focus to #main-content", true);
  });

  test("tab order on the sign-in form is logical (email -> submit)", async ({ page }) => {
    await page.goto("/en/sign-in");
    // autoFocus already puts focus on the email field (see test above); start the
    // sequence from there rather than fighting the browser's focus-navigation anchor.
    const start = await page.evaluate(() => document.activeElement?.tagName);

    const sequence: string[] = [start === "INPUT" ? "input:email" : `${start}`];
    for (let i = 0; i < 2; i++) {
      await page.keyboard.press("Tab");
      const desc = await page.evaluate(() => {
        const el = document.activeElement as HTMLElement | null;
        if (!el) return "none";
        if (el.tagName === "A") return `link:${el.getAttribute("href")}`;
        if (el.tagName === "INPUT") return `input:${el.getAttribute("type")}`;
        if (el.tagName === "BUTTON") return `button:${el.textContent?.trim()}`;
        return el.tagName;
      });
      sequence.push(desc);
    }
    expect(sequence[0]).toBe("input:email");
    expect(sequence[1]).toMatch(/^button:/);
    record("sign-in form tab order (from autofocused field)", true, sequence.join(" -> "));
  });

  test("focus is visible on interactive elements (sign-in email field)", async ({ page }) => {
    await page.goto("/en/sign-in");
    await page.getByLabel(/work email/i).focus();
    const outline = await page.evaluate(() => {
      const el = document.activeElement as HTMLElement;
      const cs = getComputedStyle(el);
      return { outlineStyle: cs.outlineStyle, outlineWidth: cs.outlineWidth, boxShadow: cs.boxShadow };
    });
    const hasVisibleRing =
      (outline.outlineStyle !== "none" && outline.outlineWidth !== "0px") || outline.boxShadow !== "none";
    expect(hasVisibleRing, `computed focus style: ${JSON.stringify(outline)}`).toBeTruthy();
    record("focus-visible ring on email field", hasVisibleRing, JSON.stringify(outline));
    await page.screenshot({ path: path.join(SCREENSHOTS_DIR, "focus-visible-email-en-light.png") });
  });

  test("command palette opens with Ctrl+K and Meta+K", async ({ browser }) => {
    const file = storageStatePath("admin-with-totp.json");
    if (!fs.existsSync(file)) {
      record("command palette: Control+K / Meta+K", false, "missing admin-with-totp.json (run 01-full-flow.spec.ts first)");
      test.skip(true, "storageState admin-with-totp.json not found");
    }
    const context = await browser.newContext({ storageState: file });
    const page = await context.newPage();
    await page.goto("/en");
    await page.waitForLoadState("load");

    await page.keyboard.press("Control+k");
    const dialog = page.getByRole("dialog");
    await expect(dialog).toBeVisible({ timeout: 5000 });
    await page.screenshot({ path: path.join(SCREENSHOTS_DIR, "command-palette-en-light.png") });
    record("command palette opens with Control+K", true);
    await page.keyboard.press("Escape");
    await expect(dialog).toBeHidden();

    await page.keyboard.press("Meta+k");
    await expect(dialog).toBeVisible({ timeout: 5000 });
    record("command palette opens with Meta+K", true);

    await page.close();
    await context.close();
  });
});
