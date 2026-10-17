// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology

import { defineConfig, devices } from "@playwright/test";

import { BASE_URL } from "../utils/origin";

// The performance pass (docs/performance.md): page timings and layout shift on a stack
// seeded by `make demo`. One worker, headless, never part of the e2e suite.
export default defineConfig({
  testDir: ".",
  testMatch: /perf\.spec\.ts$/,
  timeout: 600_000,
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: [["list"]],
  use: { baseURL: BASE_URL, headless: true, screenshot: "off", trace: "off", video: "off" },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"], viewport: { width: 1440, height: 1000 } } }],
});
