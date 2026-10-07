// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology

import { defineConfig, devices } from "@playwright/test";

// M0 audit suite for the Pairly web app. Runs against the already-running compose stack
// (`PROFILES="web" make dev`), not a dev server Playwright manages itself: the flows here
// depend on the API, Postgres and Mailpit containers too.
//
// Single worker, single browser, headless: the host machine overheats under load and the
// tests are inherently sequential anyway (one spends real Mailpit/TOTP wall-clock time
// setting up state that later specs reuse via storageState).
export default defineConfig({
  testDir: "./tests",
  timeout: 45_000,
  expect: { timeout: 10_000 },
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: [["list"], ["json", { outputFile: "./report.json" }]],
  use: {
    // The compose web service. Override with PAIRLY_E2E_BASE_URL (that origin must be in
    // PAIRLY_AUTH_TRUSTED_ORIGINS or be PAIRLY_PUBLIC_WEB_URL itself).
    baseURL: process.env.PAIRLY_E2E_BASE_URL ?? "http://127.0.0.1:3000",
    headless: true,
    screenshot: "off",
    trace: "retain-on-failure",
    video: "off",
  },
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
  ],
});
