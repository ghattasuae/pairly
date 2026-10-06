// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
import path from "node:path";
import { fileURLToPath } from "node:url";

import react from "@vitejs/plugin-react";
import { defineProject } from "vitest/config";

const here = path.dirname(fileURLToPath(import.meta.url));

// Used as a project by the root vitest.config.ts, so `pnpm test` at the root runs these
// tests exactly once, in jsdom.
export default defineProject({
  plugins: [react()],
  resolve: {
    alias: {
      "@": path.join(here, "src"),
      // Server-only modules are never imported by tests; make the guard a no-op anyway.
      "server-only": path.join(here, "test/empty.ts"),
    },
  },
  test: {
    name: "web",
    root: here,
    environment: "jsdom",
    globals: true,
    setupFiles: ["./test/setup.ts"],
    include: ["src/**/*.test.{ts,tsx}"],
    css: false,
  },
});
