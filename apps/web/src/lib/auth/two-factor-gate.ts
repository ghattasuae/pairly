// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology

/**
 * Second factor for passwordless sign-ins.
 *
 * Better Auth's two-factor plugin only intercepts credential sign-ins (`/sign-in/email`,
 * `/sign-in/username`, `/sign-in/phone-number`). Pairly signs staff in with a magic link
 * or SSO, which would otherwise skip TOTP entirely. This plugin closes that gap.
 *
 * After `/magic-link/verify` or an SSO callback creates a session for a user who has 2FA
 * enabled, it does what the two-factor plugin does for credential sign-ins:
 *
 * 1. deletes the new session (row and cookie), so no usable session exists yet;
 * 2. stores a short-lived, single-use 2FA challenge (verification row plus attempt
 *    counter, the exact records the two-factor plugin's `verify-totp` endpoint expects);
 * 3. sets the signed `two_factor` cookie that points at the challenge;
 * 4. redirects to the locale's `/two-factor` page, where the user enters a TOTP or
 *    backup code. Only a valid code creates the real session. A same-site callback page
 *    (for example an invitation) travels along as `?next=` so the user lands there after.
 *
 * There is no "trust this device" shortcut: every passwordless sign-in asks for the code.
 */
import type { BetterAuthPlugin } from "better-auth";
import { createAuthMiddleware } from "better-auth/api";
import { deleteSessionCookie } from "better-auth/cookies";
import { generateRandomString } from "better-auth/crypto";

import { defaultLocale, isLocale } from "@/i18n/routing";

import { safeNextPath } from "../safe-path";

/** Must match the two-factor plugin's cookie name (`TWO_FACTOR_COOKIE_NAME`). */
export const TWO_FACTOR_COOKIE = "two_factor";
/** Seconds the user has to enter the code. Matches the two-factor plugin default. */
export const TWO_FACTOR_CHALLENGE_SECONDS = 600;

const PASSWORDLESS_PATHS = new Set(["/magic-link/verify", "/callback/:id", "/oauth2/callback/:id"]);

/** The locale prefix of a same-site callback path such as `/ar/inbox`, else the default. */
export function localeFromCallback(callbackURL: unknown): string {
  if (typeof callbackURL !== "string") return defaultLocale;
  let path: string;
  try {
    path = new URL(decodeURIComponent(callbackURL), "http://placeholder.invalid").pathname;
  } catch {
    return defaultLocale;
  }
  const first = path.split("/")[1];
  return isLocale(first) ? first : defaultLocale;
}

/** The callback page to continue to after the code, when it is more than the locale root. */
export function nextFromCallback(callbackURL: unknown): string | null {
  if (typeof callbackURL !== "string") return null;
  try {
    const path = safeNextPath(new URL(decodeURIComponent(callbackURL), "http://placeholder.invalid").pathname);
    return path && path.split("/").length > 2 ? path : null;
  } catch {
    return null;
  }
}

export const twoFactorGate = () =>
  ({
    id: "pairly-two-factor-gate",
    hooks: {
      after: [
        {
          matcher: (context) => PASSWORDLESS_PATHS.has(context.path ?? ""),
          handler: createAuthMiddleware(async (ctx) => {
            const created = ctx.context.newSession;
            if (!created) return;
            const user = created.user as typeof created.user & { twoFactorEnabled?: boolean | null };
            if (!user.twoFactorEnabled) return;

            deleteSessionCookie(ctx, true);
            await ctx.context.internalAdapter.deleteSession(created.session.token);
            ctx.context.setNewSession(null);

            const identifier = `2fa-${generateRandomString(32)}`;
            const expiresAt = new Date(Date.now() + TWO_FACTOR_CHALLENGE_SECONDS * 1000);
            await ctx.context.internalAdapter.createVerificationValue({
              value: user.id,
              identifier,
              expiresAt,
            });
            await ctx.context.internalAdapter.createVerificationValue({
              value: "0",
              identifier: `2fa-attempts-${identifier}`,
              expiresAt,
            });
            const cookie = ctx.context.createAuthCookie(TWO_FACTOR_COOKIE, {
              maxAge: TWO_FACTOR_CHALLENGE_SECONDS,
            });
            await ctx.setSignedCookie(cookie.name, identifier, ctx.context.secret, cookie.attributes);

            const query = (ctx.query ?? {}) as Record<string, unknown>;
            const locale = localeFromCallback(query.callbackURL);
            const target = new URL(`/${locale}/two-factor`, ctx.context.baseURL);
            const next = nextFromCallback(query.callbackURL);
            if (next) target.searchParams.set("next", next);
            // Returned, not thrown, so the cookie headers set above stay on the response.
            return ctx.redirect(target.toString());
          }),
        },
      ],
    },
  }) satisfies BetterAuthPlugin;
