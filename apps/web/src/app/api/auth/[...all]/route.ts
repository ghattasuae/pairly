// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
import { toNextJsHandler } from "better-auth/next-js";

import { getAuth } from "@/lib/auth/config";
import { isBlockedAuthPath } from "@/lib/auth/blocked-paths";

// Better Auth endpoints: magic link, 2FA, SSO callbacks and /jwks. /token and the
// organization endpoints are closed to browsers (lib/auth/blocked-paths.ts).
// The instance is created on the first request, so builds never need secrets.
export const dynamic = "force-dynamic";

function notFound(): Response {
  return new Response("Not Found", { status: 404, headers: { "Cache-Control": "no-store" } });
}

export async function GET(request: Request): Promise<Response> {
  if (isBlockedAuthPath(new URL(request.url).pathname)) return notFound();
  return toNextJsHandler(getAuth()).GET(request);
}

export async function POST(request: Request): Promise<Response> {
  if (isBlockedAuthPath(new URL(request.url).pathname)) return notFound();
  return toNextJsHandler(getAuth()).POST(request);
}
