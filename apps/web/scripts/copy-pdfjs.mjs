// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology

/**
 * Copy the PDF.js worker and standard fonts from pdfjs-dist into public/pdfjs/, so the
 * document viewer loads them from our own origin (the Content Security Policy allows
 * same-origin workers only). Runs before `dev`, `build` and Storybook. The output is
 * generated, not committed (see .gitignore).
 */
import { cpSync, mkdirSync, rmSync } from "node:fs";
import { createRequire } from "node:module";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const require = createRequire(import.meta.url);
const pkg = path.dirname(require.resolve("pdfjs-dist/package.json"));
const out = path.join(here, "..", "public", "pdfjs");

rmSync(out, { recursive: true, force: true });
mkdirSync(path.join(out, "standard_fonts"), { recursive: true });
cpSync(path.join(pkg, "build", "pdf.worker.min.mjs"), path.join(out, "pdf.worker.min.mjs"));
cpSync(path.join(pkg, "standard_fonts"), path.join(out, "standard_fonts"), { recursive: true });
cpSync(path.join(pkg, "LICENSE"), path.join(out, "LICENSE"));
console.log(`copied pdf.js worker and fonts to ${path.relative(process.cwd(), out)}`);
