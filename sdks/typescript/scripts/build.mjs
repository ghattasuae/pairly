#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology

/**
 * Build `@pairly/sdk` twice with tsc: ESM (`dist/esm`) and CommonJS (`dist/cjs`, with a
 * `package.json` that marks it CommonJS), each with its declarations. No bundler.
 */
import { spawnSync } from "node:child_process";
import { mkdirSync, rmSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const pkg = resolve(dirname(fileURLToPath(import.meta.url)), "..");
rmSync(resolve(pkg, "dist"), { recursive: true, force: true });
for (const config of ["tsconfig.json", "tsconfig.cjs.json"]) {
  const result = spawnSync("tsc", ["-p", config], { cwd: pkg, stdio: "inherit", shell: process.platform === "win32" });
  if (result.status !== 0) process.exit(result.status ?? 1);
}
mkdirSync(resolve(pkg, "dist/cjs"), { recursive: true });
writeFileSync(resolve(pkg, "dist/cjs/package.json"), `${JSON.stringify({ type: "commonjs" }, null, 2)}\n`);
writeFileSync(resolve(pkg, "dist/esm/package.json"), `${JSON.stringify({ type: "module" }, null, 2)}\n`);
console.log("built dist/esm and dist/cjs");
