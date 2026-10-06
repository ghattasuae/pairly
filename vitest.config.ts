// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
import { defineConfig } from "vitest/config";

// One `vitest run` at the root runs every JS test exactly once. Each project picks its own
// environment: node for the libraries, jsdom (apps/web/vitest.config.ts) for the web app.
// apps/web has no `test` script of its own, so `pnpm -r test` does not run it twice.
export default defineConfig({
  test: {
    passWithNoTests: true,
    projects: [
      {
        test: {
          name: "libs",
          environment: "node",
          include: ["packages/shared-ts/src/**/*.test.ts", "sdks/typescript/src/**/*.test.ts"],
          exclude: ["**/node_modules/**", "**/dist/**"],
        },
      },
      "apps/web/vitest.config.ts",
    ],
    coverage: {
      provider: "v8",
      reporter: ["text", "html", "lcov"],
      include: ["packages/shared-ts/src/**", "sdks/typescript/src/**", "apps/web/src/**"],
      exclude: ["**/*.test.*", "**/node_modules/**", "**/.next/**", "**/generated/**", "**/*.stories.*"],
    },
  },
});
