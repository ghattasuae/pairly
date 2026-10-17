// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology

/** Hosted demo sign-in, server side: the API call and the magic link (lib/demo/policy.ts). */
import "server-only";

import { getAuth } from "../auth/config";
import { captureMagicLink } from "../auth/demo-capture";
import { serverEnv } from "../env";
import { type Locale } from "@/i18n/routing";

import { type DemoAccount, demoSigninEnabled, parseAccounts } from "./policy";

const TIMEOUT_MS = 5_000;

export function demoSigninOn(): boolean {
  return demoSigninEnabled(process.env);
}

/** The accounts the API offers now; `[]` when the demo sign-in is off or the API fails. */
export async function fetchDemoAccounts(): Promise<DemoAccount[]> {
  if (!demoSigninOn()) return [];
  try {
    const response = await fetch(`${serverEnv().apiInternalUrl}/v1/demo/signin-accounts`, {
      headers: { Accept: "application/json" },
      cache: "no-store",
      redirect: "error",
      signal: AbortSignal.timeout(TIMEOUT_MS),
    });
    if (!response.ok) return [];
    return parseAccounts(await response.json());
  } catch {
    return [];
  }
}

/** A magic link for `email`, captured instead of emailed (lib/auth/demo-capture.ts). */
export async function issueDemoLink(email: string, locale: Locale, headers: Headers): Promise<string | null> {
  try {
    return await captureMagicLink(email, () =>
      getAuth().api.signInMagicLink({
        body: { email, callbackURL: `/${locale}`, metadata: { locale } },
        headers,
      }),
    );
  } catch {
    return null;
  }
}
