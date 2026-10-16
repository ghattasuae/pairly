// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology

/**
 * Smoke render of every story in a built Storybook (`storybook-static/`): serves it on a
 * free local port, opens each story's iframe in headless Chromium and fails when a story
 * shows Storybook's error display, renders nothing, or throws an uncaught error.
 *
 *   pnpm --filter @pairly/web test:storybook     # build, then this
 *   node scripts/storybook-smoke.mjs [dir]        # an existing build
 *
 * One browser, one page, stories in order: light on a machine that overheats.
 */
import { createReadStream, existsSync, readFileSync, statSync } from "node:fs";
import { createServer } from "node:http";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { chromium } from "@playwright/test";

const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(process.argv[2] ?? path.join(here, "..", "storybook-static"));
const RENDER_TIMEOUT_MS = 15_000;

const TYPES = {
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript",
  ".mjs": "text/javascript",
  ".css": "text/css",
  ".json": "application/json",
  ".svg": "image/svg+xml",
  ".png": "image/png",
  ".woff2": "font/woff2",
  ".pdf": "application/pdf",
  ".bcmap": "application/octet-stream",
  ".pfb": "application/octet-stream",
  ".ttf": "font/ttf",
  ".wasm": "application/wasm",
};

function serve(dir) {
  const server = createServer((req, res) => {
    const url = new URL(req.url ?? "/", "http://localhost");
    const target = path.normalize(path.join(dir, decodeURIComponent(url.pathname)));
    if (!target.startsWith(dir)) {
      res.writeHead(403).end();
      return;
    }
    const file = existsSync(target) && statSync(target).isDirectory() ? path.join(target, "index.html") : target;
    if (!existsSync(file)) {
      res.writeHead(404).end();
      return;
    }
    res.writeHead(200, { "Content-Type": TYPES[path.extname(file)] ?? "application/octet-stream" });
    createReadStream(file).pipe(res);
  });
  return new Promise((resolve) => server.listen(0, "127.0.0.1", () => resolve(server)));
}

async function main() {
  const indexFile = path.join(root, "index.json");
  if (!existsSync(indexFile)) {
    console.error(`no Storybook build at ${root} (run build-storybook first)`);
    return 2;
  }
  const index = JSON.parse(readFileSync(indexFile, "utf8"));
  const stories = Object.values(index.entries).filter((entry) => entry.type === "story");
  const server = await serve(root);
  const { port } = server.address();
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage();
  const failures = [];
  try {
    for (const story of stories) {
      const errors = [];
      const onError = (err) => errors.push(err.message);
      page.on("pageerror", onError);
      await page.goto(`http://127.0.0.1:${port}/iframe.html?id=${encodeURIComponent(story.id)}&viewMode=story`);
      let problem = null;
      try {
        // Storybook marks the body once the story has rendered or failed.
        await page.waitForFunction(
          () =>
            document.body.classList.contains("sb-show-main") ||
            document.body.classList.contains("sb-show-errordisplay") ||
            document.body.classList.contains("sb-show-nopreview"),
          undefined,
          { timeout: RENDER_TIMEOUT_MS },
        );
        // Let effects settle (pdf.js, portals) before judging.
        await page.waitForTimeout(250);
        const state = await page.evaluate(() => ({
          error: document.body.classList.contains("sb-show-errordisplay")
            ? (document.querySelector("#error-message")?.textContent ?? "error display").slice(0, 300)
            : null,
          nopreview: document.body.classList.contains("sb-show-nopreview"),
          empty: (document.querySelector("#storybook-root")?.innerHTML.trim().length ?? 0) === 0,
        }));
        if (state.error) problem = `error display: ${state.error}`;
        else if (state.nopreview) problem = "no preview";
        else if (state.empty) problem = "rendered nothing";
      } catch {
        problem = "did not render in time";
      }
      page.off("pageerror", onError);
      if (!problem && errors.length > 0) problem = `uncaught: ${errors[0].slice(0, 300)}`;
      if (problem) failures.push({ id: story.id, problem });
      console.log(`${problem ? "FAIL" : "ok  "} ${story.id}${problem ? `  ${problem}` : ""}`);
    }
  } finally {
    await browser.close();
    server.close();
  }
  console.log(`\n${stories.length - failures.length}/${stories.length} stories rendered`);
  return failures.length === 0 ? 0 : 1;
}

process.exitCode = await main();
