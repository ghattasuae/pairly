// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology

import path from "node:path";

import { AUTH_STATE_DIR } from "../utils/report";

export type Locale = "en" | "ar";
export type Theme = "light" | "dark";
export const LOCALES: Locale[] = ["en", "ar"];
export const THEMES: Theme[] = ["light", "dark"];

export interface PageDescriptor {
  name: string;
  path: string; // without locale prefix, e.g. "/sign-in"
  /** storageState file (in AUTH_STATE_DIR) to reuse, produced by 01-full-flow.spec.ts. */
  storageState?: string;
}

export const PAGES: PageDescriptor[] = [
  { name: "sign-in", path: "/sign-in" },
  { name: "check-email", path: "/check-email" },
  { name: "two-factor", path: "/two-factor" },
  { name: "invite-invalid", path: "/invite#token=not-a-real-token-00000000000000000000000" },
  // A top-level unknown path redirects to sign-in first (the proxy's unauthenticated
  // fast-path gate only treats /sign-in, /check-email, /two-factor, /invite as public).
  // Nest the bogus segment under a public prefix so the request reaches the real
  // catch-all route ([locale]/[...rest]) and gets an actual 404.
  { name: "not-found", path: "/sign-in/this-page-does-not-exist-42" },
  { name: "home-not-provisioned", path: "/", storageState: "admin-not-provisioned.json" },
  { name: "home-set-up-2fa", path: "/", storageState: "admin-pre-totp.json" },
  { name: "home-ready", path: "/", storageState: "admin-with-totp.json" },
  { name: "members", path: "/settings/members", storageState: "admin-with-totp.json" },
];

export function storageStatePath(file: string): string {
  return path.join(AUTH_STATE_DIR, file);
}
