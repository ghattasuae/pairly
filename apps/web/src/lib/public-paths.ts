// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology

/** Pages reachable without a session (path after the locale prefix). */
const PUBLIC_PATHS = ["/sign-in", "/check-email", "/two-factor", "/invite"];

export function isPublicPath(pathAfterLocale: string): boolean {
  return PUBLIC_PATHS.some((p) => pathAfterLocale === p || pathAfterLocale.startsWith(`${p}/`));
}
