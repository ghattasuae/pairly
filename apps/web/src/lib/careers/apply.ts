// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology

/**
 * The apply flow's wire format between the browser and our route handlers, shared by
 * both sides. Pure, no I/O.
 *
 * The browser talks only to this origin (`/api/careers/...`); the route handlers pass
 * the request on to the API with the visitor's address. Each handler accepts only
 * same-origin requests carrying `X-Pairly-Apply: 1`, which a form or link on another
 * site cannot send (see `checkApplyRequest`).
 */

export const APPLY_HEADER = "x-pairly-apply";
/** Largest JSON body an apply route accepts (answers are short). */
export const MAX_JSON_BYTES = 64 * 1024;
/** The API's limit for one application file. */
export const MAX_CV_BYTES = 20 * 1024 * 1024;
export const ACCEPTED_FILES = ".pdf,.docx,.png,.jpg,.jpeg";

export function applyPath(orgSlug: string, jobSlug: string): string {
  return `/api/careers/${orgSlug}/${jobSlug}/apply`;
}
export function filesPath(orgSlug: string): string {
  return `/api/careers/${orgSlug}/files`;
}
export function submitPath(orgSlug: string): string {
  return `/api/careers/${orgSlug}/submit`;
}

export type ApplyRefusal = { status: number; code: string };

/** Same origin and our custom header, else a refusal. `trustedOrigins` are exact origins. */
export function checkApplyRequest(headers: Pick<Headers, "get">, trustedOrigins: readonly string[]): ApplyRefusal | null {
  const site = headers.get("sec-fetch-site");
  const origin = headers.get("origin");
  if (site !== null && site !== "same-origin") return { status: 403, code: "cross_site" };
  if (origin !== null && !trustedOrigins.includes(origin)) return { status: 403, code: "cross_site" };
  if (site === null && origin === null) return { status: 403, code: "cross_site" };
  if (headers.get(APPLY_HEADER) !== "1") return { status: 403, code: "cross_site" };
  return null;
}

export interface ApplyBody {
  consent: { notice_version: string; accepted: boolean };
  contact: {
    full_name: string;
    email: string;
    phone: string | null;
    location: string | null;
    profile_url: string | null;
    locale: "en" | "ar";
  };
  answers: Record<string, string>;
  knockouts: Record<string, "yes" | "no">;
  self_identification: Record<string, string>;
  turnstile_token: string;
}

function text(value: unknown, max: number): string | null {
  return typeof value === "string" && value.length <= max ? value : null;
}

function record(value: unknown, maxKeys: number, maxLength: number): Record<string, string> | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const entries = Object.entries(value as Record<string, unknown>);
  if (entries.length > maxKeys) return null;
  const out: Record<string, string> = {};
  for (const [key, v] of entries) {
    if (!/^[a-z][a-z0-9_]{0,47}$/.test(key) || typeof v !== "string" || v.length > maxLength) return null;
    out[key] = v;
  }
  return out;
}

/** The checked apply body, or null when anything is off. The API checks everything again. */
export function parseApplyBody(raw: unknown): ApplyBody | null {
  if (!raw || typeof raw !== "object") return null;
  const body = raw as Record<string, unknown>;
  const consent = body.consent as Record<string, unknown> | undefined;
  const contact = body.contact as Record<string, unknown> | undefined;
  if (!consent || !contact) return null;
  const version = text(consent.notice_version, 32);
  const name = text(contact.full_name, 200);
  const email = text(contact.email, 320);
  const optional = (v: unknown, max: number) => (v === null || v === undefined || v === "" ? null : text(v, max));
  const phone = optional(contact.phone, 60);
  const location = optional(contact.location, 200);
  const profile = optional(contact.profile_url, 400);
  const answers = record(body.answers ?? {}, 40, 2000);
  const knockouts = record(body.knockouts ?? {}, 10, 3);
  const selfId = record(body.self_identification ?? {}, 10, 40);
  const token = text(body.turnstile_token, 2048);
  if (
    version === null ||
    typeof consent.accepted !== "boolean" ||
    name === null ||
    email === null ||
    (contact.phone && phone === null) ||
    (contact.location && location === null) ||
    (contact.profile_url && profile === null) ||
    answers === null ||
    knockouts === null ||
    Object.values(knockouts).some((v) => v !== "yes" && v !== "no") ||
    selfId === null ||
    token === null
  ) {
    return null;
  }
  return {
    consent: { notice_version: version, accepted: consent.accepted },
    contact: {
      full_name: name,
      email,
      phone,
      location,
      profile_url: profile,
      locale: contact.locale === "ar" ? "ar" : "en",
    },
    answers,
    knockouts: knockouts as Record<string, "yes" | "no">,
    self_identification: selfId,
    turnstile_token: token,
  };
}

const TOKEN = /^[A-Za-z0-9_-]{20,100}$/;

export function isUploadToken(value: unknown): value is string {
  return typeof value === "string" && TOKEN.test(value);
}

/** Error codes the apply flow explains in plain words (anything else: "unavailable"). */
export const KNOWN_CODES = [
  "consent_required",
  "bot_check_failed",
  "disposable_email",
  "invalid_email",
  "rate_limited",
  "cv_required",
  "too_large",
  "payload_too_large",
  "unsupported_media_type",
  "not_found",
  "invalid_input",
  "cross_site",
] as const;

export type KnownCode = (typeof KNOWN_CODES)[number] | "unavailable";

export function knownCode(code: unknown): KnownCode {
  return typeof code === "string" && (KNOWN_CODES as readonly string[]).includes(code) ? (code as KnownCode) : "unavailable";
}
