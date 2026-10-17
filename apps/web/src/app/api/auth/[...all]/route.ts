// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
import { toNextJsHandler } from "better-auth/next-js";

import { getAuth } from "@/lib/auth/config";
import { isBlockedAuthPath, isDemoBlockedAuthPath } from "@/lib/auth/blocked-paths";
import { demoSigninEnabled } from "@/lib/demo/policy";

// Better Auth endpoints: magic link, 2FA, SSO callbacks and /jwks. /token and the
// organization endpoints are closed to browsers (lib/auth/blocked-paths.ts), and on the
// hosted demo the endpoints that would change a shared demo account.
// The instance is created on the first request, so builds never need secrets.
export const dynamic = "force-dynamic";

function blocked(request: Request): boolean {
  const { pathname } = new URL(request.url);
  return isBlockedAuthPath(pathname) || isDemoBlockedAuthPath(pathname, demoSigninEnabled(process.env));
}

function notFound(): Response {
  return new Response("Not Found", { status: 404, headers: { "Cache-Control": "no-store" } });
}

export async function GET(request: Request): Promise<Response> {
  if (blocked(request)) return notFound();
  return toNextJsHandler(getAuth()).GET(request);
}

export async function POST(request: Request): Promise<Response> {
  if (blocked(request)) return notFound();
  return toNextJsHandler(getAuth()).POST(request);
}
