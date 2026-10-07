// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
import path from "node:path";
import { fileURLToPath } from "node:url";

import type { StorybookConfig } from "@storybook/nextjs-vite";

const here = path.dirname(fileURLToPath(import.meta.url));

const config: StorybookConfig = {
  framework: { name: "@storybook/nextjs-vite", options: {} },
  stories: ["../src/**/*.stories.@(ts|tsx)"],
  addons: ["@storybook/addon-a11y", "@storybook/addon-themes"],
  staticDirs: ["../public"],
  core: { disableTelemetry: true },
  // Server actions reach the API with a server-side token: never bundle them for the
  // browser here. Every `@/lib/actions/*` import gets the same harmless stand-in.
  viteFinal: async (vite) => {
    const mock = path.join(here, "actions-mock.ts");
    const existing = vite.resolve?.alias;
    const list = Array.isArray(existing)
      ? existing
      : Object.entries(existing ?? {}).map(([find, replacement]) => ({ find, replacement: String(replacement) }));
    return {
      ...vite,
      resolve: { ...vite.resolve, alias: [{ find: /^@\/lib\/actions\/.*$/, replacement: mock }, ...list] },
    };
  },
};

export default config;
