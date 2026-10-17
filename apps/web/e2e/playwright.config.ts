// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology

import { defineConfig, devices } from "@playwright/test";

import { BASE_URL } from "./utils/origin";

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
    // The compose web service at http://localhost:<PAIRLY_WEB_PORT>, the magic-link host
    // (see utils/origin.ts). Override with PAIRLY_E2E_BASE_URL.
    baseURL: BASE_URL,
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
