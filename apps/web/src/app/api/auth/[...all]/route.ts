// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
import { toNextJsHandler } from "better-auth/next-js";

import { getAuth } from "@/lib/auth/config";

// Better Auth endpoints: magic link, 2FA, organizations, SSO callbacks, /token and /jwks.
// The instance is created on the first request, so builds never need secrets.
export const dynamic = "force-dynamic";

export async function GET(request: Request): Promise<Response> {
  return toNextJsHandler(getAuth()).GET(request);
}

export async function POST(request: Request): Promise<Response> {
  return toNextJsHandler(getAuth()).POST(request);
}
