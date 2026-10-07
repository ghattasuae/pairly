// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
import js from "@eslint/js";
import nextPlugin from "@next/eslint-plugin-next";
import { defineConfig } from "eslint/config";
import reactHooks from "eslint-plugin-react-hooks";
import globals from "globals";
import tseslint from "typescript-eslint";

export default defineConfig(
  {
    ignores: [
      "**/node_modules/**",
      ".claude/**",
      "**/dist/**",
      "**/coverage/**",
      "**/.next/**",
      "**/storybook-static/**",
      "**/next-env.d.ts",
      "**/generated/**",
      ".venv/**",
    ],
  },
  js.configs.recommended,
  tseslint.configs.recommended,
  {
    languageOptions: {
      ecmaVersion: 2023,
      sourceType: "module",
      globals: { ...globals.node },
    },
  },
  // Web app: React hooks rules, Next.js rules, browser globals.
  {
    files: ["apps/web/**/*.{ts,tsx,mjs}"],
    plugins: { "@next/next": nextPlugin, "react-hooks": reactHooks },
    languageOptions: { globals: { ...globals.browser, ...globals.node } },
    settings: { next: { rootDir: "apps/web/" } },
    rules: {
      ...nextPlugin.configs.recommended.rules,
      ...nextPlugin.configs["core-web-vitals"].rules,
      ...reactHooks.configs.recommended.rules,
      "@typescript-eslint/no-unused-vars": ["error", { argsIgnorePattern: "^_", varsIgnorePattern: "^_" }],
      "@typescript-eslint/consistent-type-imports": ["error", { fixStyle: "inline-type-imports" }],
    },
  },
);
