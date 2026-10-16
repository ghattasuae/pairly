// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology

// Shared scratch-space paths and a tiny NDJSON logger used by every spec so the final
// report (pass/fail table, a11y violations, copy findings) can be assembled after the
// whole run from plain files, without relying on parsing Playwright's own reporters.
import { appendFileSync, mkdirSync } from "node:fs";
import path from "node:path";

export const SCRATCH_ROOT =
  process.env.PAIRLY_E2E_SCRATCH ??
  "/private/tmp/claude-501/-Users-ghattas-Desktop-Pairly/529c7938-e698-475d-a449-d90cb918599f/scratchpad";

export const SCREENSHOTS_DIR = path.join(SCRATCH_ROOT, "m0-screens");
export const AUTH_STATE_DIR = path.join(SCRATCH_ROOT, "auth-state");
export const FINDINGS_DIR = path.join(SCRATCH_ROOT, "findings");

for (const dir of [SCREENSHOTS_DIR, AUTH_STATE_DIR, FINDINGS_DIR]) mkdirSync(dir, { recursive: true });

function appendNdjson(file: string, record: unknown): void {
  appendFileSync(path.join(FINDINGS_DIR, file), `${JSON.stringify(record)}\n`);
}

export function logAxe(record: {
  page: string;
  locale: string;
  theme: string;
  violations: { id: string; impact: string | null; help: string; nodes: string[] }[];
}): void {
  appendNdjson("axe.ndjson", record);
}

export function logCopy(record: {
  page: string;
  locale: string;
  theme: string;
  emDash: boolean;
  rawKeys: string[];
  suspiciousEnglish: string[];
}): void {
  appendNdjson("copy.ndjson", record);
}

export function logRtl(record: {
  page: string;
  htmlDir: string | null;
  sidebarOnRight: boolean | null;
  overflow375: boolean;
  overflow1280: boolean;
}): void {
  appendNdjson("rtl.ndjson", record);
}

export function logSecurity(record: Record<string, unknown>): void {
  appendNdjson("security.ndjson", record);
}

export function logCheck(record: { name: string; status: "pass" | "fail"; detail?: string }): void {
  appendNdjson("checks.ndjson", record);
}
