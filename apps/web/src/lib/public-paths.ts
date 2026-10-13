// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology

/** Pages reachable without a session (path after the locale prefix). */
// `/approvals/link` is public so a signed-out person sees what the link is for and can
// sign in from there (the token stays in the fragment, see the page). `/careers` holds
// the organisations' public careers pages and the apply flow (Pairly Hire), and
// `/candidate/preferences` a candidate's email preferences (signed link in the fragment),
// `/schedule` a candidate's interview scheduling page (signed link in the fragment).
// `/scorecard` is an interviewer's scorecard opened from its one-click link (its own
// HTTP-only session cookie for one interview; the API decides every access).
// `/reference` is a referee's form opened from its signed link (its own HTTP-only session
// cookie for one reference request; the API decides every access).
// `/portal` is the candidate portal: candidates never have a staff session; the portal
// checks its own session (an HTTP-only cookie) and the API decides every access.
const PUBLIC_PATHS = [
  "/sign-in",
  "/check-email",
  "/two-factor",
  "/invite",
  "/unsubscribe",
  "/approvals/link",
  "/careers",
  "/candidate",
  "/portal",
  "/schedule",
  "/scorecard",
  "/reference",
];

export function isPublicPath(pathAfterLocale: string): boolean {
  return PUBLIC_PATHS.some((p) => pathAfterLocale === p || pathAfterLocale.startsWith(`${p}/`));
}
