// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology

/**
 * Runs before every page request:
 *
 * 1. Content Security Policy with a fresh nonce per request (Next applies it to its own
 *    scripts; next-themes gets it through the x-nonce header).
 * 2. Optimistic sign-in gate: no session cookie on a protected page means a redirect to
 *    sign-in. This is only a fast path. The (app) layout validates the session for real,
 *    and the API validates the token on every call.
 * 3. Locale routing (next-intl): `/` goes to `/en` or `/ar`, unknown locales get one.
 */
import { getSessionCookie } from "better-auth/cookies";
import { NextRequest, NextResponse } from "next/server";
import createMiddleware from "next-intl/middleware";

import { isLocale, routing } from "./i18n/routing";
import { COOKIE_PREFIX } from "./lib/auth/constants";
import { buildCsp, documentOriginFrom } from "./lib/csp";
import { isPublicPath } from "./lib/public-paths";
import { safeNextPath } from "./lib/safe-path";

const handleI18nRouting = createMiddleware(routing);

export function proxy(request: NextRequest): NextResponse {
  const nonce = Buffer.from(crypto.randomUUID()).toString("base64");
  const dev = process.env.NODE_ENV === "development";
  const csp = buildCsp({
    nonce,
    dev,
    https: request.nextUrl.protocol === "https:",
    documentOrigin: documentOriginFrom(process.env.PAIRLY_DOCUMENT_ORIGIN, dev, process.env.MINIO_API_HOST_PORT),
  });

  const segments = request.nextUrl.pathname.split("/");
  const locale = segments[1];
  if (isLocale(locale)) {
    const rest = `/${segments.slice(2).join("/")}`.replace(/\/$/, "") || "/";
    if (!isPublicPath(rest) && !getSessionCookie(request, { cookiePrefix: COOKIE_PREFIX })) {
      const url = request.nextUrl.clone();
      url.pathname = `/${locale}/sign-in`;
      url.search = "";
      // Come back to the same page after signing in (a plain same-site path only).
      const next = safeNextPath(request.nextUrl.pathname);
      if (next && next !== `/${locale}`) url.searchParams.set("next", next);
      const redirect = NextResponse.redirect(url);
      redirect.headers.set("Content-Security-Policy", csp);
      return redirect;
    }
  }

  const headers = new Headers(request.headers);
  headers.set("x-nonce", nonce);
  headers.set("Content-Security-Policy", csp);
  const response = handleI18nRouting(new NextRequest(request, { headers }));
  response.headers.set("Content-Security-Policy", csp);
  return response;
}

export const config = {
  // Everything except API routes, Next internals and files with an extension.
  matcher: [
    {
      source: "/((?!api|_next/static|_next/image|.*\\..*).*)",
      missing: [
        { type: "header", key: "next-router-prefetch" },
        { type: "header", key: "purpose", value: "prefetch" },
      ],
    },
  ],
};
