// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology

/**
 * `POST /api/demo/signin` (form: `email`, `locale`): the hosted demo sign-in
 * (lib/demo/policy.ts). Signs the visitor in as one of the demo users the API offers by
 * redirecting to a fresh magic link; nothing is emailed. 404 unless the demo sign-in is
 * on; 403 for a post from another site.
 */
import { defaultLocale, isLocale } from "@/i18n/routing";
import { clientIpFrom } from "@/lib/careers/format";
import { DemoSigninLimiter, demoSignin } from "@/lib/demo/policy";
import { demoSigninOn, fetchDemoAccounts, issueDemoLink } from "@/lib/demo/server";
import { serverEnv } from "@/lib/env";

export const dynamic = "force-dynamic";

const limiter = new DemoSigninLimiter();
const NO_STORE = { "Cache-Control": "no-store" };

function redirect(location: string): Response {
  return new Response(null, { status: 303, headers: { ...NO_STORE, Location: location } });
}

export async function POST(request: Request): Promise<Response> {
  const enabled = demoSigninOn();
  if (!enabled) return new Response("Not Found", { status: 404, headers: NO_STORE });
  let form: FormData;
  try {
    form = await request.formData();
  } catch {
    return new Response("Bad Request", { status: 400, headers: NO_STORE });
  }
  const rawLocale = form.get("locale");
  const locale = typeof rawLocale === "string" && isLocale(rawLocale) ? rawLocale : defaultLocale;
  const rawEmail = form.get("email");
  const email = typeof rawEmail === "string" ? rawEmail.slice(0, 320) : "";
  const outcome = await demoSignin({
    enabled,
    origin: request.headers.get("origin"),
    trustedOrigins: serverEnv().trustedOrigins,
    clientKey: clientIpFrom(request.headers) ?? "unknown",
    email,
    limiter,
    fetchAccounts: fetchDemoAccounts,
    issueLink: (address) => issueDemoLink(address, locale, request.headers),
  });
  switch (outcome.kind) {
    case "redirect":
      return redirect(outcome.location);
    case "not_found":
      return new Response("Not Found", { status: 404, headers: NO_STORE });
    case "forbidden":
      return new Response("Forbidden", { status: 403, headers: NO_STORE });
    case "error":
      return redirect(`/${locale}/demo?error=${outcome.code}`);
  }
}
