// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    include: [
      "packages/shared-ts/src/**/*.test.ts",
      "sdks/typescript/src/**/*.test.ts",
      "apps/web/**/*.test.{ts,tsx}",
    ],
    exclude: ["**/node_modules/**", "**/dist/**", "**/.next/**"],
    passWithNoTests: true,
    coverage: {
      provider: "v8",
      reporter: ["text", "html", "lcov"],
      include: ["packages/shared-ts/src/**", "sdks/typescript/src/**", "apps/web/**"],
      exclude: ["**/*.test.*", "**/node_modules/**", "**/.next/**"],
    },
  },
});
