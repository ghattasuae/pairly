// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology

/** Pages reachable without a session (path after the locale prefix). */
// `/approvals/link` is public so a signed-out person sees what the link is for and can
// sign in from there (the token stays in the fragment, see the page). `/careers` holds
// the organisations' public careers pages and the apply flow (Pairly Hire), and
// `/candidate/preferences` a candidate's email preferences (signed link in the fragment).
const PUBLIC_PATHS = [
  "/sign-in",
  "/check-email",
  "/two-factor",
  "/invite",
  "/unsubscribe",
  "/approvals/link",
  "/careers",
  "/candidate",
];

export function isPublicPath(pathAfterLocale: string): boolean {
  return PUBLIC_PATHS.some((p) => pathAfterLocale === p || pathAfterLocale.startsWith(`${p}/`));
}
