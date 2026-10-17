// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Proofly Technology

/**
 * Webhook helpers: verify a delivery, parse it, and build signed test events.
 *
 * Every delivery carries `Pairly-Signature: t=<unix seconds>,v1=<hex>`, where `<hex>` is
 * `HMAC-SHA256(secret, "<t>." + body)` over the exact body bytes, keyed with the
 * endpoint's signing secret (`whsec_...`). This mirrors the engine
 * (`pairly_core.webhooks.signature`) with Web Crypto only, so it runs on Node 20+ and on
 * edge runtimes. Always verify the raw body, before parsing it.
 *
 * ```ts
 * const event = await constructEvent(await request.text(), request.headers.get("Pairly-Signature"), secret);
 * if (event.type === "batch.completed") console.log(event.data.counts);
 * ```
 */

export const SIGNATURE_HEADER = "Pairly-Signature";
export const EVENT_ID_HEADER = "Pairly-Event-Id";
export const EVENT_TYPE_HEADER = "Pairly-Event-Type";
export const DEFAULT_TOLERANCE_SECONDS = 300;
const MAX_HEADER_LENGTH = 4096;
const encoder = new TextEncoder();

export class WebhookSignatureError extends Error {
  override name = "WebhookSignatureError";
}

function bytesOf(payload: string | Uint8Array): Uint8Array {
  return typeof payload === "string" ? encoder.encode(payload) : payload;
}

function hex(buffer: ArrayBuffer): string {
  return Array.from(new Uint8Array(buffer), (b) => b.toString(16).padStart(2, "0")).join("");
}

/** Constant-time comparison of two strings (no early exit on the first difference). */
function safeEqual(a: string, b: string): boolean {
  let diff = a.length ^ b.length;
  const length = Math.max(a.length, b.length);
  for (let i = 0; i < length; i += 1) {
    diff |= (a.charCodeAt(i) || 0) ^ (b.charCodeAt(i) || 0);
  }
  return diff === 0;
}

/** Hex `HMAC-SHA256(secret, "<timestamp>." + payload)`. */
export async function computeSignature(
  secret: string,
  timestamp: number,
  payload: string | Uint8Array,
): Promise<string> {
  const key = await crypto.subtle.importKey(
    "raw",
    encoder.encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const prefix = encoder.encode(`${Math.trunc(timestamp)}.`);
  const body = bytesOf(payload);
  const signed = new Uint8Array(prefix.length + body.length);
  signed.set(prefix, 0);
  signed.set(body, prefix.length);
  return hex(await crypto.subtle.sign("HMAC", key, signed));
}

/** The `Pairly-Signature` header value for `payload` (for tests and tools). */
export async function signPayload(
  secret: string,
  payload: string | Uint8Array,
  timestamp: number = Math.floor(Date.now() / 1000),
): Promise<string> {
  const t = Math.trunc(timestamp);
  return `t=${t},v1=${await computeSignature(secret, t, payload)}`;
}

export interface VerifyOptions {
  toleranceSeconds?: number;
  /** Unix seconds; default now. */
  now?: number;
}

/**
 * Check a `Pairly-Signature` header against the raw body. Resolves to its timestamp, or
 * rejects with `WebhookSignatureError` when the header is missing or malformed, the
 * timestamp is more than the tolerance away from now (replay protection), or no `v1`
 * value matches (several are allowed during a secret rotation).
 */
export async function verifySignature(
  payload: string | Uint8Array,
  header: string | null | undefined,
  secret: string,
  options: VerifyOptions = {},
): Promise<number> {
  if (!secret) throw new WebhookSignatureError("no signing secret configured");
  if (!header || header.length > MAX_HEADER_LENGTH) {
    throw new WebhookSignatureError("missing or oversized signature header");
  }
  let timestamp: number | null = null;
  const candidates: string[] = [];
  for (const item of header.split(",")) {
    const trimmed = item.trim();
    const sep = trimmed.indexOf("=");
    if (sep < 0) throw new WebhookSignatureError("malformed signature header");
    const key = trimmed.slice(0, sep);
    const value = trimmed.slice(sep + 1);
    if (key === "t") {
      if (timestamp !== null || !/^\d{1,12}$/.test(value)) {
        throw new WebhookSignatureError("malformed signature timestamp");
      }
      timestamp = Number(value);
    } else if (key === "v1") {
      candidates.push(value);
    }
  }
  if (timestamp === null || candidates.length === 0) {
    throw new WebhookSignatureError("the signature header needs t and v1");
  }
  const tolerance = options.toleranceSeconds ?? DEFAULT_TOLERANCE_SECONDS;
  const now = options.now ?? Math.floor(Date.now() / 1000);
  if (tolerance < 0 || Math.abs(now - timestamp) > tolerance) {
    throw new WebhookSignatureError("the signature timestamp is outside the tolerance");
  }
  const expected = await computeSignature(secret, timestamp, payload);
  let matched = false;
  for (const candidate of candidates) matched = safeEqual(expected, candidate) || matched;
  if (!matched) throw new WebhookSignatureError("no signature matches");
  return timestamp;
}

/** The engine's name for the same function. */
export const verifyWebhookSignature = verifySignature;

// ------------------------------------------------------------------ typed events

export interface ApprovalDecidedData {
  approval_id: string;
  run_id: string;
  workflow: string;
  approval_type: string;
  step: string;
  decision_id: string;
  decision: "approve" | "reject" | "request_changes";
  approval_status: string;
  entity_type: string | null;
  entity_id: string | null;
}

export interface DocumentReceivedData {
  document_id: string;
  workflow: string;
  source: string;
  media_type: string;
  size_bytes: number;
  sha256: string;
}

export interface DecisionRecordedData {
  decision_id: string;
  kind: string;
  workflow: string | null;
  entity_type: string | null;
  entity_id: string | null;
  final_action: string | null;
  supersedes: string | null;
  content_hash: string;
  decided_at: string;
}

export interface BatchCompletedData {
  batch_id: string;
  workflow: string;
  status: string;
  metadata: Record<string, unknown>;
  counts: Record<string, number>;
  items: Array<{
    id: string;
    position: number;
    source: string;
    status: string;
    reason: string | null;
    document_id: string | null;
    entity_type: string | null;
    entity_id: string | null;
  }>;
}

export interface EventDataMap {
  "approval.decided": ApprovalDecidedData;
  "document.received": DocumentReceivedData;
  "decision.recorded": DecisionRecordedData;
  "batch.completed": BatchCompletedData;
}

export type EventType = keyof EventDataMap;

interface EventBase<T extends string, D> {
  id: string;
  type: T;
  created_at: string;
  org_id: string;
  data: D;
}

/**
 * A delivery. Narrow on `type` to get typed `data`. Pairly may add event types later:
 * keep a default branch that ignores types you do not know.
 */
export type WebhookEvent = { [K in EventType]: EventBase<K, EventDataMap[K]> }[EventType];

/** Parse a verified body. Throws `TypeError` when it is not an event. */
export function parseEvent(payload: string | Uint8Array): WebhookEvent {
  const text = typeof payload === "string" ? payload : new TextDecoder().decode(payload);
  let raw: unknown;
  try {
    raw = JSON.parse(text);
  } catch {
    throw new TypeError("the webhook body is not JSON");
  }
  const event = raw as Record<string, unknown> | null;
  if (
    typeof event !== "object" ||
    event === null ||
    typeof event["id"] !== "string" ||
    typeof event["type"] !== "string" ||
    typeof event["created_at"] !== "string" ||
    typeof event["org_id"] !== "string" ||
    typeof event["data"] !== "object" ||
    event["data"] === null
  ) {
    throw new TypeError("the webhook body is not an event");
  }
  return event as unknown as WebhookEvent;
}

/** Verify the signature, then parse: the one call a webhook handler needs. */
export async function constructEvent(
  payload: string | Uint8Array,
  header: string | null | undefined,
  secret: string,
  options: VerifyOptions = {},
): Promise<WebhookEvent> {
  await verifySignature(payload, header, secret, options);
  return parseEvent(payload);
}

// ------------------------------------------------------------------ building events

function sortKeys(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(sortKeys);
  if (value !== null && typeof value === "object") {
    return Object.fromEntries(
      Object.keys(value)
        .sort()
        .map((key) => [key, sortKeys((value as Record<string, unknown>)[key])]),
    );
  }
  return value;
}

/** The exact body Pairly sends (and signs): JSON with sorted keys and no spaces. */
export function serialise(payload: unknown): string {
  return JSON.stringify(sortKeys(payload));
}

export function buildEvent<K extends EventType>(
  type: K,
  data: EventDataMap[K],
  options: { orgId: string; eventId?: string; createdAt?: Date },
): EventBase<K, EventDataMap[K]> {
  const created = (options.createdAt ?? new Date()).toISOString().replace(/\.\d{3}Z$/, "Z");
  return {
    id: options.eventId ?? `evt_${crypto.randomUUID().replace(/-/g, "")}`,
    type,
    created_at: created,
    org_id: options.orgId,
    data,
  };
}

/** The body and headers Pairly would send for `event` (to test your handler). */
export async function signedDelivery(
  event: { id: string; type: string },
  secret: string,
  timestamp?: number,
): Promise<{ body: string; headers: Record<string, string> }> {
  const body = serialise(event);
  return {
    body,
    headers: {
      "Content-Type": "application/json",
      [SIGNATURE_HEADER]: await signPayload(secret, body, timestamp),
      [EVENT_ID_HEADER]: event.id,
      [EVENT_TYPE_HEADER]: event.type,
    },
  };
}
