// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology

/**
 * Better Auth for Pairly.
 *
 * - Sign-in: email magic link (SMTP; Mailpit locally). Google and Microsoft SSO are wired
 *   but only switched on when their client id and secret are both set.
 * - TOTP 2FA (two-factor plugin) with backup codes, enforced on magic link and SSO
 *   sign-ins by `twoFactorGate`.
 * - Organizations plugin: the org switcher and the `X-Pairly-Org` header come from here.
 * - JWT plugin: short-lived EdDSA tokens for the FastAPI API, keys published at
 *   `/api/auth/jwks`. Issuer and audience must equal the API's `PAIRLY_AUTH_ISSUER` and
 *   `PAIRLY_AUTH_AUDIENCE`. Tokens carry only standard claims: the API loads the user by
 *   `sub` and decides every permission itself.
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

import { COOKIE_PREFIX } from "./constants";
import { twoFactorGate } from "./two-factor-gate";

/** Seconds a magic link stays valid. The email copy says 5 minutes. */
export const MAGIC_LINK_SECONDS = 300;
/** Lifetime of an API token. The API allows 10 seconds of clock skew on top. */
export const API_TOKEN_LIFETIME = "5m";

/** The URL in the email opens our confirm page, not the verify endpoint directly. */
export function confirmUrl(baseUrl: string, locale: Locale, token: string): string {
  const url = new URL(`/${locale}/sign-in/confirm`, baseUrl);
  // Fragment, not query: it never reaches server logs or a Referer header.
  url.hash = new URLSearchParams({ token }).toString();
  return url.toString();
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
    session: {
      expiresIn: 60 * 60 * 24 * 3,
      updateAge: 60 * 60 * 12,
      freshAge: 60 * 15,
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
        "/token": { window: 60, max: 120 },
        "/jwks": { window: 60, max: 300 },
      },
    },
    emailAndPassword: { enabled: false },
    socialProviders: {
      ...(env.google
        ? { google: { clientId: env.google.clientId, clientSecret: env.google.clientSecret } }
        : {}),
      ...(env.microsoft
        ? {
            microsoft: {
              clientId: env.microsoft.clientId,
              clientSecret: env.microsoft.clientSecret,
              tenantId: env.microsoft.tenantId,
            },
          }
        : {}),
    },
    plugins: [
      magicLink({
        expiresIn: MAGIC_LINK_SECONDS,
        storeToken: "hashed",
        disableSignUp: !env.allowSignUp,
        sendMagicLink: async ({ email, token, metadata }) => {
          const requested = (metadata as { locale?: unknown } | undefined)?.locale;
          const locale = isLocale(requested) ? requested : defaultLocale;
          await sendMagicLinkEmail({ to: email, url: confirmUrl(env.baseUrl, locale, token), locale });
        },
      }),
      twoFactor({
        issuer: "Pairly",
        // Staff have no password: enabling and disabling 2FA needs a fresh session instead.
        allowPasswordless: true,
      }),
      twoFactorGate(),
      organization({
        // Provisioning gap (see apps/web/README.md): organizations are created by an
        // admin process that also creates them in the API, never by end users here.
        allowUserToCreateOrganization: false,
      }),
      jwt({
        jwks: { keyPairConfig: { alg: "EdDSA", crv: "Ed25519" } },
        jwt: {
          issuer: env.jwtIssuer,
          audience: env.jwtAudience,
          expirationTime: API_TOKEN_LIFETIME,
          // Standard claims only (sub, iat, exp, iss, aud). No profile data in tokens.
          definePayload: () => ({}),
        },
        // Tokens are minted server side through /token only; never leak them in headers.
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
