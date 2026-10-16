// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology

/**
 * The viewport of the demo specs' staff pages: `fallback` (1440x1000), or
 * `PAIRLY_E2E_VIEWPORT` written as WIDTHxHEIGHT (the demo recordings use 1280x800,
 * see playwright.record.config.ts).
 */
export function demoViewport(fallback = { width: 1440, height: 1000 }): { width: number; height: number } {
  const found = /^(\d{3,4})x(\d{3,4})$/.exec(process.env.PAIRLY_E2E_VIEWPORT ?? "");
  return found ? { width: Number(found[1]), height: Number(found[2]) } : fallback;
}
