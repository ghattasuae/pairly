// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology

/**
 * Configuration, retries and error decoding, shared by every call.
 *
 * Retries: 429 (honouring `Retry-After`), 500, 502, 503, 504 and network errors, with
 * capped exponential backoff and full jitter, at most `maxRetries` times. Every POST
 * gets an `Idempotency-Key` (one per call, kept across its retries), so a retried POST
 * is never applied twice by the endpoints that honour it. No Node-only API is used, so
 * this runs on Node 20+, Deno, Bun, Cloudflare Workers and the Vercel edge runtime.
 */

import { PairlyAPIError, PairlyConfigError, PairlyConnectionError } from "./errors.js";

export const SDK_VERSION = "0.1.0";
export const DEFAULT_BASE_URL = "http://127.0.0.1:8000";
export const ORG_HEADER = "X-Pairly-Org";
const RETRY_STATUSES = new Set([429, 500, 502, 503, 504]);
const MAX_RETRY_AFTER_MS = 60_000;
const LOOPBACK = new Set(["127.0.0.1", "localhost", "[::1]", "::1"]);
const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export interface RetryPolicy {
  maxRetries: number;
  baseDelayMs: number;
  maxDelayMs: number;
  /** A number in [0, 1); `Math.random` by default (full jitter). */
  jitter: () => number;
}

export interface PairlyOptions {
  /** An API key (`pk_live_...` or `pk_test_...`). */
  apiKey?: string;
  /** Or a person's JWT, with `orgId`. */
  token?: string;
  orgId?: string;
  /** Default `http://127.0.0.1:8000` (the native stack). https unless it is this machine. */
  baseUrl?: string;
  /** Allow plain http to a host other than this machine (tests only). */
  allowHttp?: boolean;
  maxRetries?: number;
  retry?: Partial<RetryPolicy>;
  timeoutMs?: number;
  /** A fetch implementation (default: the global `fetch`). */
  fetch?: (input: Request) => Promise<Response>;
  /** Waits between retries (default: setTimeout). */
  sleep?: (ms: number) => Promise<void>;
}

export interface ResolvedConfig {
  baseUrl: string;
  headers: Record<string, string>;
  retry: RetryPolicy;
  timeoutMs: number;
  fetch: (input: Request) => Promise<Response>;
  sleep: (ms: number) => Promise<void>;
}

const defaultSleep = (ms: number): Promise<void> => new Promise((resolve) => setTimeout(resolve, ms));

export function resolveConfig(options: PairlyOptions): ResolvedConfig {
  const { apiKey, token } = options;
  if ((apiKey === undefined) === (token === undefined)) {
    throw new PairlyConfigError("pass exactly one of apiKey and token");
  }
  if (apiKey !== undefined && !/^pk_(live|test)_/.test(apiKey)) {
    throw new PairlyConfigError("apiKey must be a Pairly API key (pk_live_... or pk_test_...)");
  }
  if (options.orgId !== undefined && !UUID_RE.test(options.orgId)) {
    throw new PairlyConfigError("orgId must be an organisation id (a UUID)");
  }
  if (token !== undefined && options.orgId === undefined) {
    throw new PairlyConfigError("a token needs orgId");
  }
  const baseUrl = (options.baseUrl ?? DEFAULT_BASE_URL).replace(/\/+$/, "");
  let url: URL;
  try {
    url = new URL(baseUrl);
  } catch {
    throw new PairlyConfigError("baseUrl must be an http(s) URL");
  }
  if (url.protocol !== "https:" && url.protocol !== "http:") {
    throw new PairlyConfigError("baseUrl must be an http(s) URL");
  }
  if (url.protocol === "http:" && !LOOPBACK.has(url.hostname) && options.allowHttp !== true) {
    throw new PairlyConfigError("baseUrl must use https unless it points at this machine");
  }
  const maxRetries = options.retry?.maxRetries ?? options.maxRetries ?? 3;
  if (!Number.isInteger(maxRetries) || maxRetries < 0) {
    throw new PairlyConfigError("maxRetries must be 0 or more");
  }
  const headers: Record<string, string> = {
    Authorization: `Bearer ${apiKey ?? token ?? ""}`,
    Accept: "application/json",
  };
  if (options.orgId !== undefined) headers[ORG_HEADER] = options.orgId;
  const globalFetch = (input: Request): Promise<Response> => fetch(input);
  return {
    baseUrl,
    headers,
    retry: {
      maxRetries,
      baseDelayMs: options.retry?.baseDelayMs ?? 500,
      maxDelayMs: options.retry?.maxDelayMs ?? 8000,
      jitter: options.retry?.jitter ?? Math.random,
    },
    timeoutMs: options.timeoutMs ?? 60_000,
    fetch: options.fetch ?? globalFetch,
    sleep: options.sleep ?? defaultSleep,
  };
}

/** Milliseconds before retry number `attempt` (1-based). */
export function retryDelay(policy: RetryPolicy, attempt: number, retryAfterMs: number | null): number {
  if (retryAfterMs !== null) return Math.min(Math.max(retryAfterMs, 0), MAX_RETRY_AFTER_MS);
  const ceiling = Math.min(policy.maxDelayMs, policy.baseDelayMs * 2 ** (attempt - 1));
  return ceiling * policy.jitter();
}

export function retryAfterMs(response: Response): number | null {
  const raw = response.headers.get("retry-after");
  if (raw === null) return null;
  const trimmed = raw.trim();
  if (/^\d+$/.test(trimmed)) return Number(trimmed) * 1000;
  const when = Date.parse(trimmed);
  return Number.isNaN(when) ? null : Math.max(when - Date.now(), 0);
}

export function newIdempotencyKey(): string {
  return `ts-${crypto.randomUUID().replace(/-/g, "")}`;
}

/**
 * A fetch that adds the credentials, an `Idempotency-Key` on POST, a timeout, and the
 * retries. `openapi-fetch` and the raw helpers both use it.
 */
export function retryingFetch(config: ResolvedConfig): (input: Request) => Promise<Response> {
  return async (input: Request): Promise<Response> => {
    const headers = new Headers(input.headers);
    for (const [name, value] of Object.entries(config.headers)) {
      if (!headers.has(name)) headers.set(name, value);
    }
    if (input.method === "POST" && !headers.has("Idempotency-Key")) {
      headers.set("Idempotency-Key", newIdempotencyKey());
    }
    const body = input.body === null ? null : await input.arrayBuffer();
    let attempt = 0;
    for (;;) {
      const signal = AbortSignal.timeout(config.timeoutMs);
      const request = new Request(input.url, { method: input.method, headers, body, signal, redirect: "manual" });
      let response: Response;
      try {
        response = await config.fetch(request);
      } catch (error) {
        attempt += 1;
        if (attempt > config.retry.maxRetries) {
          throw new PairlyConnectionError(`could not reach the Pairly API: ${String(error)}`);
        }
        await config.sleep(retryDelay(config.retry, attempt, null));
        continue;
      }
      if (RETRY_STATUSES.has(response.status) && attempt < config.retry.maxRetries) {
        attempt += 1;
        await config.sleep(retryDelay(config.retry, attempt, retryAfterMs(response)));
        continue;
      }
      return response;
    }
  };
}

/** The JSON body, or a `PairlyAPIError` built from the problem details. */
export async function decode<T>(response: Response): Promise<T> {
  const text = await response.text();
  let body: unknown = null;
  if (text) {
    try {
      body = JSON.parse(text) as unknown;
    } catch {
      body = text;
    }
  }
  if (response.status >= 400) throw problem(response, body);
  return body as T;
}

export function problem(response: Response, body: unknown): PairlyAPIError {
  const fields = typeof body === "object" && body !== null ? (body as Record<string, unknown>) : {};
  const text = (value: unknown): string | null => (typeof value === "string" ? value : null);
  return new PairlyAPIError({
    status: response.status,
    title: text(fields["title"]) ?? (response.statusText || "Error"),
    detail: text(fields["detail"]) ?? "",
    code: text(fields["code"]),
    requestId: text(fields["request_id"]) ?? response.headers.get("x-request-id"),
    retryAfterMs: retryAfterMs(response),
    body,
  });
}

export function uuidSegment(value: string, what: string): string {
  if (!UUID_RE.test(value)) throw new TypeError(`${what} must be an id (a UUID)`);
  return value.toLowerCase();
}
