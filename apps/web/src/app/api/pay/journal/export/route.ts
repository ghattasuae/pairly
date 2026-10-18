// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology

/**
 * The journal of a period as CSV (`POST /v1/pay/journal/export`), passed through. The
 * API checks `pay.journal.export` and the TOTP step-up, records the download with the
 * file's SHA-256 and format and audits it. Only our own pages may start it: the
 * month-end page sends the `X-Pairly-Export` header, which a link or form on another
 * site cannot.
 *
 * `?format=` picks the layout (`generic` when absent). For `xero`, `quickbooks` and
 * `zoho_books` the JSON body carries `accounts` (journal account to the account of the
 * accounting system) and, for Xero, `tax_rate`; the API validates both.
 */
import { isSameOriginRequest, jsonProblem, upstream, upstreamProblem } from "@/lib/api/bff";
import { NotSignedInError } from "@/lib/api/server";

export const dynamic = "force-dynamic";

const PERIOD_RE = /^\d{4}-(0[1-9]|1[0-2])$/;
const FORMATS = new Set(["generic", "xero", "quickbooks", "zoho_books"]);
const MAX_BODY = 64 * 1024;

type Options = { accounts?: Record<string, string>; tax_rate?: string };

/** The optional body: an object with string `accounts` and `tax_rate`, nothing else. */
async function readOptions(request: Request): Promise<Options | null> {
  const text = await request.text();
  if (text.length > MAX_BODY) return null;
  if (text.trim() === "") return {};
  let body: unknown;
  try {
    body = JSON.parse(text);
  } catch {
    return null;
  }
  if (typeof body !== "object" || body === null || Array.isArray(body)) return null;
  const out: Options = {};
  for (const [key, value] of Object.entries(body)) {
    if (key === "tax_rate" && typeof value === "string") out.tax_rate = value;
    else if (key === "accounts" && typeof value === "object" && value !== null && !Array.isArray(value)) {
      const entries = Object.entries(value);
      if (!entries.every(([, account]) => typeof account === "string")) return null;
      out.accounts = Object.fromEntries(entries) as Record<string, string>;
    } else return null;
  }
  return out;
}

export async function POST(request: Request): Promise<Response> {
  if (!isSameOriginRequest(request) || request.headers.get("x-pairly-export") !== "1") {
    return jsonProblem(403, "cross_site");
  }
  const params = new URL(request.url).searchParams;
  const period = params.get("period") ?? "";
  if (!PERIOD_RE.test(period)) return jsonProblem(400, "invalid_period");
  const format = params.get("format") ?? "generic";
  if (!FORMATS.has(format)) return jsonProblem(400, "invalid_format");
  const options = await readOptions(request);
  if (options === null) return jsonProblem(400, "invalid_request");
  // The generic export sends exactly what it always did.
  const json = format === "generic" ? { period, ...options } : { period, format, ...options };
  const filename = format === "generic" ? `journal-${period}.csv` : `journal-${period}-${format}.csv`;
  let response: Response;
  try {
    response = await upstream("/v1/pay/journal/export", { accept: "text/csv", json, signal: request.signal });
  } catch (err) {
    if (err instanceof NotSignedInError) return jsonProblem(401, "not_signed_in");
    return jsonProblem(502, "unavailable");
  }
  if (!response.ok || !response.body) return upstreamProblem(response);
  return new Response(response.body, {
    headers: {
      "Content-Type": "text/csv; charset=utf-8",
      "Content-Disposition": `attachment; filename="${filename}"`,
      "Cache-Control": "private, no-store",
    },
  });
}
