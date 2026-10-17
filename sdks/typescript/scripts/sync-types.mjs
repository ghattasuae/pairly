#!/usr/bin/env node
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Proofly Technology

/**
 * Copy the generated OpenAPI types (`packages/shared-ts/src/generated/api.ts`) into
 * `src/generated/api.ts`, so `@pairly/sdk` has no workspace dependency and publishes on
 * its own. The copy is gitignored and made before every build and typecheck; the
 * source of truth stays `pnpm gen:api`.
 *
 * The SDK is Apache-2.0 (LICENSING.md), so the copy's SPDX line is rewritten from the
 * server's dual license to Apache-2.0. Proofly Technology holds the copyright in the
 * generated types and licenses the SDK copy under Apache-2.0.
 */
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const SPDX_RE = /^\/\/ SPDX-License-Identifier: .*$/m;
const APACHE_LINE = "// SPDX-License-Identifier: Apache-2.0";

const pkg = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const source = resolve(pkg, "../../packages/shared-ts/src/generated/api.ts");
const target = resolve(pkg, "src/generated/api.ts");

if (!existsSync(source)) {
  console.error(`sync-types: ${source} is missing; run pnpm gen:api`);
  process.exit(1);
}
const original = readFileSync(source, "utf8");
const head = original.split("\n", 3).join("\n");
if (!SPDX_RE.test(head)) {
  console.error(`sync-types: ${source} has no SPDX line in its header; run pnpm gen:api`);
  process.exit(1);
}
const wanted = original.replace(SPDX_RE, APACHE_LINE);
mkdirSync(dirname(target), { recursive: true });
if (!existsSync(target) || readFileSync(target, "utf8") !== wanted) {
  writeFileSync(target, wanted);
  console.log("sync-types: copied the generated API types into src/generated/api.ts (Apache-2.0)");
}
