// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology

/**
 * Better Auth for Pairly.
 *
 * - Staff are invite-only. Nobody signs up here: the magic link and SSO never create a
 *   user. A Better Auth user is created only for the address of a valid invitation
 *   (lib/invitations.ts), and access comes only from accepting it in the API.
 * - Sign-in: email magic link (SMTP; Mailpit locally), limited per IP by Better Auth and
 *   per address by `takeMagicLinkQuota`. Links go only to existing users; every request
 *   gets the same answer. Google and Microsoft SSO are wired but only switched on when
 *   their client id and secret are both set.
 * - TOTP 2FA (two-factor plugin) with backup codes, enforced on magic link and SSO
 *   sign-ins by `twoFactorGate`. Each session records how it was signed in (`amr`).
 * - Organizations plugin: only mirrors the API organisation that is active in the session
 *   (`X-Pairly-Org`). Its HTTP endpoints are blocked (lib/auth/blocked-paths.ts).
 * - JWT plugin: short-lived EdDSA tokens for the FastAPI API, keys published at
 *   `/api/auth/jwks`. Issuer and audience must equal the API's `PAIRLY_AUTH_ISSUER` and
 *   `PAIRLY_AUTH_AUDIENCE`. Claims: `sub`, `amr` (`["otp"]` only after a TOTP code),
 *   `email` and `email_verified` (to bind invitations). The API decides every
 *   permission itself. Tokens are minted in-process only: `/token` is closed to HTTP.
 * - Postgres as role `pairly_auth`, whose search path is schema `auth`. IDs are UUIDs, so
 *   an organization id can be sent to the API as `X-Pairly-Org` unchanged.
 *
 * This module has no `server-only` import so the migration script (tsx) can load it. App
 * code imports `@/lib/auth/server` instead, which does.
 */
import { betterAuth } from "better-auth";
import { jwt, magicLink, organization, twoFactor } from "better-auth/plugins";
import { Pool } from "pg";

import { isLocale, type Locale, defaultLocale } from "@/i18n/routing";

import { sendMagicLinkEmail } from "../email";
import { type ServerEnv, serverEnv } from "../env";
import { safeNextPath } from "../safe-path";

import { amrClaim, amrForPath } from "./amr";
import { COOKIE_PREFIX } from "./constants";
import { magicLinkLimitSchema, takeMagicLinkQuota } from "./magic-link-limit";
import { twoFactorGate } from "./two-factor-gate";

/** Seconds a magic link stays valid. The email copy says 5 minutes. */
export const MAGIC_LINK_SECONDS = 300;
/** Lifetime of an API token. The API allows 10 seconds of clock skew on top. */
export const API_TOKEN_LIFETIME = "5m";

/**
 * The URL in the email opens our confirm page, not the verify endpoint directly. `next`
 * (a same-site page such as an invitation) is where the person lands after signing in.
 */
export function confirmUrl(baseUrl: string, locale: Locale, token: string, next?: string | null): string {
  const url = new URL(`/${locale}/sign-in/confirm`, baseUrl);
  const params = new URLSearchParams({ token });
  const safe = safeNextPath(next);
  if (safe) params.set("next", safe);
  // Fragment, not query: it never reaches server logs or a Referer header.
  url.hash = params.toString();
  return url.toString();
}

/** The `callbackURL` Better Auth put in its own verify URL, if it is a safe page path. */
export function callbackFromVerifyUrl(verifyUrl: string): string | null {
  try {
    return safeNextPath(new URL(verifyUrl).searchParams.get("callbackURL"));
  } catch {
    return null;
  }
}

function createAuth(env: ServerEnv) {
  const pool = new Pool({
    connectionString: env.databaseUrl,
    max: 5,
    // Fail fast instead of hanging a request when Postgres is down.
    connectionTimeoutMillis: 5000,
    idleTimeoutMillis: 30_000,
    statement_timeout: 10_000,
  });
  const secureCookies = env.baseUrl.startsWith("https://");

  return betterAuth({
    appName: "Pairly",
    baseURL: env.baseUrl,
    basePath: "/api/auth",
    secret: env.secret,
    trustedOrigins: [new URL(env.baseUrl).origin],
    database: pool,
    telemetry: { enabled: false },
    advanced: {
      cookiePrefix: COOKIE_PREFIX,
      useSecureCookies: secureCookies,
      defaultCookieAttributes: { httpOnly: true, sameSite: "lax", secure: secureCookies },
      database: { generateId: "uuid" },
      ipAddress: { ipAddressHeaders: ["x-forwarded-for", "x-real-ip"] },
    },
    rateLimit: {
      enabled: true,
      storage: "database",
      window: 60,
      max: 60,
      customRules: {
        "/sign-in/magic-link": { window: 300, max: 5 },
        "/magic-link/verify": { window: 60, max: 10 },
        "/two-factor/*": { window: 60, max: 5 },
        "/jwks": { window: 60, max: 300 },
      },
    },
    // The browser never mints API tokens over HTTP; see lib/auth/blocked-paths.ts.
    disabledPaths: ["/token"],
    emailAndPassword: { enabled: false },
    session: {
      expiresIn: 60 * 60 * 24 * 3,
      updateAge: 60 * 60 * 12,
      freshAge: 60 * 15,
      // How this session was signed in (lib/auth/amr.ts). Set by the hook below only.
      additionalFields: { amr: { type: "string", required: false, input: false } },
    },
    databaseHooks: {
      session: {
        create: {
          before: async (session, context) => ({ data: { ...session, amr: amrForPath(context?.path) } }),
        },
      },
    },
    // SSO signs in existing (invited) users only; it never creates one.
    socialProviders: {
      ...(env.google
        ? { google: { clientId: env.google.clientId, clientSecret: env.google.clientSecret, disableSignUp: true } }
        : {}),
      ...(env.microsoft
        ? {
            microsoft: {
              clientId: env.microsoft.clientId,
              clientSecret: env.microsoft.clientSecret,
              tenantId: env.microsoft.tenantId,
              disableSignUp: true,
            },
          }
        : {}),
    },
    plugins: [
      magicLinkLimitSchema(),
      magicLink({
        expiresIn: MAGIC_LINK_SECONDS,
        storeToken: "hashed",
        // Invite-only: never create a user from a magic link.
        disableSignUp: true,
        sendMagicLink: async ({ email, url, token, metadata }, ctx) => {
          // Same answer for every request (Better Auth returns {status: true} after this):
          // over the per-address limit, or no such user, means no email and no error.
          if (!(await takeMagicLinkQuota(pool, email))) return;
          const user = await ctx?.context.internalAdapter.findUserByEmail(email);
          if (!user) return;
          const requested = (metadata as { locale?: unknown } | undefined)?.locale;
          const locale = isLocale(requested) ? requested : defaultLocale;
          const link = confirmUrl(env.baseUrl, locale, token, callbackFromVerifyUrl(url));
          await sendMagicLinkEmail({ to: email, url: link, locale });
        },
      }),
      twoFactor({
        issuer: "Pairly",
        // Staff have no password: enabling and disabling 2FA needs a fresh session instead.
        allowPasswordless: true,
      }),
      twoFactorGate(),
      organization({
        // Organizations mirror the API's (same UUID) and are written in-process by
        // lib/org-mirror.ts after the API confirms a membership. Never by end users.
        allowUserToCreateOrganization: false,
      }),
      jwt({
        jwks: { keyPairConfig: { alg: "EdDSA", crv: "Ed25519" } },
        jwt: {
          issuer: env.jwtIssuer,
          audience: env.jwtAudience,
          expirationTime: API_TOKEN_LIFETIME,
          // sub, iat, exp, iss and aud are added by the plugin. No other profile data.
          definePayload: ({ user, session }) => ({
            amr: amrClaim((session as { amr?: unknown }).amr),
            email: user.email,
            email_verified: user.emailVerified === true,
          }),
        },
        // Tokens are minted in-process only (auth.api.getToken); never leak them in headers.
        disableSettingJwtHeader: true,
      }),
    ],
  });
}

export type Auth = ReturnType<typeof createAuth>;

let instance: Auth | undefined;

/** The Better Auth instance, created on first use (never at build time). */
export function getAuth(): Auth {
  instance ??= createAuth(serverEnv());
  return instance;
}
