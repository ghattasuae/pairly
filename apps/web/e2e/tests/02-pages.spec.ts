import fs from "node:fs";
import path from "node:path";

import { test, expect } from "@playwright/test";

import { auditA11y } from "../utils/axe";
import { auditCopy } from "../utils/copy";
import { auditRtl } from "../utils/rtl";
import { LOCALES, PAGES, storageStatePath, THEMES } from "../utils/pages";
import { SCREENSHOTS_DIR, logCheck } from "../utils/report";

// The reusable page catalog (item 1 in the brief) across en/ar x light/dark, with axe,
// copy and RTL audits folded in (items 3, 4, 6) since every combination needs the same
// page load anyway. Session-bound pages reuse storageState snapshots written by
// 01-full-flow.spec.ts; skip them with a clear failure if that file did not produce one.

for (const descriptor of PAGES) {
  for (const locale of LOCALES) {
    for (const theme of THEMES) {
      const name = `${descriptor.name}-${locale}-${theme}`;

      test(`page: ${name}`, async ({ browser }) => {
        if (descriptor.storageState) {
          const file = storageStatePath(descriptor.storageState);
          if (!fs.existsSync(file)) {
            logCheck({ name, status: "fail", detail: `missing storageState ${descriptor.storageState} (full-flow spec did not reach this state)` });
            test.skip(true, `storageState ${descriptor.storageState} not found`);
          }
        }

        const context = await browser.newContext({
          storageState: descriptor.storageState ? storageStatePath(descriptor.storageState) : undefined,
          colorScheme: theme,
        });
        const page = await context.newPage();
        await page.emulateMedia({ colorScheme: theme });

        const url = `/${locale}${descriptor.path}`;
        const response = await page.goto(url);

        await page.waitForLoadState("networkidle").catch(() => undefined);

        // Structural (locale-agnostic) checks always apply; English-text checks only for en
        // (the ar page shows the same structure with Arabic copy, asserted separately by
        // the copy/RTL audits below).
        switch (descriptor.name) {
          case "not-found":
            expect(response?.status()).toBe(404);
            if (locale === "en") await expect(page.getByText(/404/)).toBeVisible();
            break;
          case "sign-in":
            if (locale === "en") await expect(page.getByRole("button", { name: /email me a link/i })).toBeVisible();
            else await expect(page.getByRole("button").first()).toBeVisible();
            break;
          case "check-email":
            await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
            break;
          case "two-factor":
            await expect(page.getByRole("button").first()).toBeVisible();
            break;
          case "invite-invalid":
            if (locale === "en") await expect(page.getByText(/cannot be used/i)).toBeVisible();
            break;
          case "members":
            expect(response?.status(), "Members page must not 404 for an admin with TOTP").not.toBe(404);
            if (locale === "en") await expect(page.getByRole("button", { name: /send invitation/i })).toBeVisible();
            break;
          default:
            break;
        }

        await page.screenshot({ path: path.join(SCREENSHOTS_DIR, `${name}.png`), fullPage: true });

        const violations = await auditA11y(page, descriptor.name, locale, theme);
        await auditCopy(page, descriptor.name, locale, theme);
        if (locale === "ar") await auditRtl(page, descriptor.name);

        logCheck({ name, status: "pass", detail: `${violations} axe violation(s)` });

        await page.close();
        await context.close();
      });
    }
  }
}
