// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology

import path from "node:path";

import { defineConfig } from "@playwright/test";

import baseConfig from "./playwright.config";

// The demo recordings (docs/demo.md, docs/media): the Hire and Pay demo specs on a
// fresh `make demo` stack, with a video of every browser context they open, at
// 1280x800. Run with PAIRLY_E2E_VIEWPORT=1280x800 (the specs' staff pages use it) and
// PAIRLY_E2E_VIDEO_DIR (where the .webm files go); scripts/demo_gif.py turns them into
// GIFs. The JSON report goes next to the videos (it dates each test, so the converter
// can leave out the sign-in set-up), never over e2e/report.json.
const VIDEO_DIR = process.env.PAIRLY_E2E_VIDEO_DIR ?? path.resolve("demo-videos");
const SIZE = { width: 1280, height: 800 };
// Every action waits this long (ms, PAIRLY_E2E_SLOWMO), so a viewer can follow it.
const SLOW_MO = Number(process.env.PAIRLY_E2E_SLOWMO ?? "400");

export default defineConfig({
  ...baseConfig,
  testMatch: [/14-hire-demo\.spec\.ts$/, /18-pay-demo\.spec\.ts$/],
  timeout: 600_000,
  reporter: [["list"], ["json", { outputFile: path.join(VIDEO_DIR, "report.json") }]],
  use: {
    ...baseConfig.use,
    viewport: SIZE,
    launchOptions: { slowMo: SLOW_MO },
    // contextOptions reach every browser.newContext() of a test, not only the page
    // fixture, so the specs' own contexts are recorded too.
    contextOptions: { recordVideo: { dir: VIDEO_DIR, size: SIZE } },
  },
  projects: [{ name: "chromium", use: { browserName: "chromium", viewport: SIZE } }],
});
