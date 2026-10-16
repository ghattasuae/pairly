#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology

/**
 * Prove `@pairly/sdk` works on its own, outside the monorepo:
 *
 * 1. build it, then `pnpm pack` the tarball;
 * 2. check the tarball's package.json has no `workspace:` dependency and that the
 *    generated types are inside it;
 * 3. in a fresh temp project (`$PAIRLY_PACK_DIR`, default the OS temp folder), `npm
 *    install` the tarball, then import it with ESM and CommonJS and typecheck a small
 *    TypeScript file against its declarations.
 *
 * The temp project is deleted at the end unless `PAIRLY_PACK_KEEP=1`.
 */
import { spawnSync } from "node:child_process";
import { mkdirSync, mkdtempSync, readdirSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const pkg = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const repo = resolve(pkg, "../..");
const tsc = resolve(repo, "node_modules/.bin/tsc");

function run(cmd, args, cwd, { capture = false } = {}) {
  const result = spawnSync(cmd, args, { cwd, encoding: "utf8", stdio: capture ? "pipe" : "inherit" });
  if (result.status !== 0) {
    if (capture) process.stderr.write(`${result.stdout ?? ""}${result.stderr ?? ""}`);
    throw new Error(`${cmd} ${args.join(" ")} failed in ${cwd}`);
  }
  return result.stdout ?? "";
}

const base = process.env.PAIRLY_PACK_DIR || tmpdir();
mkdirSync(base, { recursive: true });
const work = mkdtempSync(join(base, "pairly-sdk-pack-"));
try {
  run("pnpm", ["run", "build"], pkg);
  const packs = join(work, "packs");
  mkdirSync(packs);
  run("pnpm", ["pack", "--pack-destination", packs], pkg, { capture: true });
  const tarball = readdirSync(packs).find((f) => f.endsWith(".tgz"));
  if (!tarball) throw new Error("pnpm pack wrote no tarball");
  const tgz = join(packs, tarball);

  const listing = run("tar", ["-tzf", tgz], work, { capture: true });
  for (const needed of ["package/dist/esm/generated/api.d.ts", "package/dist/cjs/index.js"]) {
    if (!listing.includes(needed)) throw new Error(`the tarball lacks ${needed}`);
  }
  const manifest = JSON.parse(run("tar", ["-xzOf", tgz, "package/package.json"], work, { capture: true }));
  const deps = { ...manifest.dependencies, ...manifest.peerDependencies, ...manifest.optionalDependencies };
  for (const [name, range] of Object.entries(deps)) {
    if (String(range).startsWith("workspace:") || name.startsWith("@pairly/")) {
      throw new Error(`the packed package still depends on ${name}@${range}`);
    }
  }

  const app = join(work, "app");
  mkdirSync(app);
  writeFileSync(join(app, "package.json"), JSON.stringify({ name: "pack-check", private: true, type: "module" }));
  run("npm", ["install", "--no-audit", "--no-fund", "--loglevel=error", tgz], app);

  writeFileSync(
    join(app, "esm.mjs"),
    [
      'import { Pairly, constructEvent, SDK_VERSION } from "@pairly/sdk";',
      'const client = new Pairly({ apiKey: "pk_test_x", baseUrl: "http://127.0.0.1:9" });',
      'if (typeof client.approvals.listMine !== "function" || typeof constructEvent !== "function") process.exit(1);',
      'console.log(`esm ok ${SDK_VERSION}`);',
    ].join("\n"),
  );
  writeFileSync(
    join(app, "cjs.cjs"),
    [
      'const { Pairly, PairlyAPIError } = require("@pairly/sdk");',
      'if (typeof Pairly !== "function" || typeof PairlyAPIError !== "function") process.exit(1);',
      'console.log("cjs ok");',
    ].join("\n"),
  );
  writeFileSync(
    join(app, "check.ts"),
    [
      'import { Pairly, type Batch } from "@pairly/sdk";',
      'const pairly = new Pairly({ apiKey: "pk_test_x" });',
      'export async function status(id: string): Promise<Batch["status"]> {',
      "  return (await pairly.documents.getBatch(id)).status;",
      "}",
    ].join("\n"),
  );
  writeFileSync(
    join(app, "tsconfig.json"),
    JSON.stringify({
      compilerOptions: {
        target: "ES2022",
        module: "NodeNext",
        moduleResolution: "NodeNext",
        strict: true,
        noEmit: true,
        skipLibCheck: false,
        lib: ["ES2023", "DOM"],
        types: [],
      },
      files: ["check.ts"],
    }),
  );
  run("node", ["esm.mjs"], app);
  run("node", ["cjs.cjs"], app);
  run(tsc, ["-p", "tsconfig.json"], app);
  console.log(`pack-check: ${tarball} installs, imports (ESM and CommonJS) and typechecks on its own`);
} finally {
  if (process.env.PAIRLY_PACK_KEEP !== "1") rmSync(work, { recursive: true, force: true });
}
