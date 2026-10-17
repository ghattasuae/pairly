#!/usr/bin/env node
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Proofly Technology

/**
 * Copy the generated OpenAPI types (`packages/shared-ts/src/generated/api.ts`) into
 * `src/generated/api.ts`, so `@pairly/sdk` has no workspace dependency and publishes on
 * its own. The copy is gitignored and made before every build and typecheck; the
 * source of truth stays `pnpm gen:api`.
 */
import { copyFileSync, existsSync, mkdirSync, readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const pkg = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const source = resolve(pkg, "../../packages/shared-ts/src/generated/api.ts");
const target = resolve(pkg, "src/generated/api.ts");

if (!existsSync(source)) {
  console.error(`sync-types: ${source} is missing; run pnpm gen:api`);
  process.exit(1);
}
mkdirSync(dirname(target), { recursive: true });
if (!existsSync(target) || readFileSync(target, "utf8") !== readFileSync(source, "utf8")) {
  copyFileSync(source, target);
  console.log("sync-types: copied the generated API types into src/generated/api.ts");
}
