// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology

/**
 * Resend's send endpoint (`POST https://api.resend.com/emails`) over `fetch`, for the web
 * app's own mail (magic links, invitations). No SDK: one JSON request, the same contract
 * the API's `ResendTransport` (pairly_core.notifications.email) follows.
 *
 * - `Authorization: Bearer <key>` (a sending-access key) and an `Idempotency-Key`, so a
 *   retry within Resend's 24 hour window never sends a second email.
 * - Retried here, with the same key: network errors, 5xx, a 409
 *   `concurrent_idempotent_requests`, and a 429 `rate_limit_exceeded` whose `Retry-After`
 *   is short. The caller is a person waiting for a sign-in link, so at most three tries
 *   within a few seconds; quota errors and long waits fail at once.
 * - Everything else (400, 401, 403, 404, 409 `invalid_idempotent_request`, 422) fails at
 *   once. The error carries Resend's status and error name only, never the key or body.
 *
 * Docs read 2026-10-19: https://resend.com/docs/api-reference/emails/send-email,
 * https://resend.com/docs/api-reference/errors, https://resend.com/docs/api-reference/rate-limit,
 * https://resend.com/docs/dashboard/emails/idempotency-keys.
 */

export const RESEND_DEFAULT_BASE_URL = "https://api.resend.com";
const MAX_ATTEMPTS = 3;
const MAX_INLINE_WAIT_MS = 2000;
const TIMEOUT_MS = 10_000;
const TRANSIENT_409 = new Set(["concurrent_idempotent_requests", "resource_locked"]);
const NAME_RE = /^[a-z_]{1,48}$/;
const TAG_RE = /^[A-Za-z0-9_-]{1,256}$/;

export interface ResendMail {
  from: string;
  to: string;
  subject: string;
  text: string;
  html: string;
  replyTo?: string | undefined;
  tags?: Record<string, string>;
}

export interface ResendOptions {
  apiKey: string;
  idempotencyKey: string;
  baseUrl?: string;
  fetchImpl?: typeof fetch;
  sleep?: (ms: number) => Promise<void>;
}

export class ResendError extends Error {
  constructor(
    readonly status: number,
    readonly errorName: string,
    readonly transient: boolean,
    readonly retryAfterMs: number | null = null,
  ) {
    super(`resend_${status}${errorName ? `_${errorName}` : ""}`);
    this.name = "ResendError";
  }
}

function oneLine(value: string, limit = 998): string {
  return value.replace(/[\r\n]+/g, " ").trim().slice(0, limit);
}

function retryAfterMs(response: Response): number | null {
  for (const name of ["retry-after", "ratelimit-reset"]) {
    const raw = response.headers.get(name)?.trim();
    if (raw && /^\d{1,6}$/.test(raw)) return Number(raw) * 1000;
  }
  return null;
}

async function errorName(response: Response): Promise<string> {
  try {
    const data: unknown = await response.json();
    const name = typeof data === "object" && data !== null ? (data as { name?: unknown }).name : undefined;
    return typeof name === "string" && NAME_RE.test(name) ? name : "";
  } catch {
    return "";
  }
}

/** Map a non-2xx answer: retried later or not, and how long Resend asked us to wait. */
export async function resendErrorFor(response: Response): Promise<ResendError> {
  const status = response.status;
  const name = await errorName(response);
  const wait = retryAfterMs(response);
  if (status === 429 || status >= 500) return new ResendError(status, name, true, wait);
  if (status === 409 && TRANSIENT_409.has(name)) return new ResendError(status, name, true, wait);
  return new ResendError(status, name, false);
}

export function resendBody(mail: ResendMail): Record<string, unknown> {
  const body: Record<string, unknown> = {
    from: oneLine(mail.from, 320),
    to: [oneLine(mail.to, 320)],
    subject: oneLine(mail.subject),
    text: mail.text,
    html: mail.html,
  };
  if (mail.replyTo) body.reply_to = [oneLine(mail.replyTo, 320)];
  const tags = Object.entries(mail.tags ?? {})
    .filter(([name, value]) => TAG_RE.test(name) && TAG_RE.test(value))
    .slice(0, 5)
    .map(([name, value]) => ({ name, value }));
  if (tags.length > 0) body.tags = tags;
  return body;
}

const defaultSleep = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms));

/** Send one email. Returns Resend's email id. Throws `ResendError`. */
export async function sendWithResend(mail: ResendMail, options: ResendOptions): Promise<string> {
  const key = oneLine(options.idempotencyKey, 256);
  if (!key) throw new ResendError(0, "invalid_idempotency_key", false);
  const doFetch = options.fetchImpl ?? fetch;
  const sleep = options.sleep ?? defaultSleep;
  const url = `${(options.baseUrl ?? RESEND_DEFAULT_BASE_URL).replace(/\/+$/, "")}/emails`;
  const body = JSON.stringify(resendBody(mail));
  let last: ResendError | undefined;
  for (let attempt = 1; attempt <= MAX_ATTEMPTS; attempt += 1) {
    let response: Response;
    try {
      response = await doFetch(url, {
        method: "POST",
        headers: {
          Authorization: `Bearer ${options.apiKey}`,
          "Content-Type": "application/json",
          "Idempotency-Key": key,
        },
        body,
        redirect: "error",
        signal: AbortSignal.timeout(TIMEOUT_MS),
      });
    } catch {
      last = new ResendError(0, "network_error", true);
      if (attempt < MAX_ATTEMPTS) await sleep(250 * 2 ** (attempt - 1));
      continue;
    }
    if (response.ok) {
      try {
        const data: unknown = await response.json();
        const id = typeof data === "object" && data !== null ? (data as { id?: unknown }).id : undefined;
        return typeof id === "string" ? id : "";
      } catch {
        return "";
      }
    }
    last = await resendErrorFor(response);
    if (!last.transient || attempt === MAX_ATTEMPTS) break;
    if (last.status === 429) {
      // Only a short per-second rate limit is worth waiting for; quotas are not.
      const wait = last.retryAfterMs;
      if (last.errorName !== "rate_limit_exceeded" || wait === null || wait > MAX_INLINE_WAIT_MS) break;
      await sleep(wait);
    } else {
      await sleep(Math.min(last.retryAfterMs ?? 250 * 2 ** (attempt - 1), MAX_INLINE_WAIT_MS));
    }
  }
  throw last ?? new ResendError(0, "unknown", false);
}
