// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology

/**
 * Pure helpers for the public careers pages and the apply flow. No I/O, so they run in
 * server components, route handlers, the browser and unit tests alike.
 *
 * - `jsonLdScript`: JSON-LD made safe to put inside a `<script>` element.
 * - `descriptionBlocks`: the plain job text (paragraphs and `- ` lists) as blocks.
 * - `salaryText`, `localTitle`, `localDescription`: what a job card and page show.
 * - `readPublicForm`: the API's public application form, checked field by field.
 * - `clientIpFrom`: the visitor's address for the API's per-IP limits.
 */
import type { components } from "@pairly/shared-ts";

export type CareersOut = components["schemas"]["HireCareersOut"];
export type PublicJob = components["schemas"]["HirePublicJob"];
export type PublicJobDetail = components["schemas"]["HirePublicJobDetail"];
export type PublicNotice = components["schemas"]["HirePublicNotice"];
export type Salary = components["schemas"]["HirePublicSalary"];

export type Locale = "en" | "ar";

/** Cloudflare's documented test token, accepted only by the API's fake verifier. */
export const DEV_TURNSTILE_TOKEN = "XXXX.DUMMY.TOKEN.XXXX";

// ------------------------------------------------------------------ JSON-LD

/**
 * Serialise JSON-LD for an inline `<script type="application/ld+json">`. `<`, `>` and `&`
 * become Unicode escapes, so no value can close the script element or open a comment,
 * and U+2028/U+2029 are escaped for old parsers. The result is still valid JSON.
 */
const LINE_SEPARATOR = String.fromCharCode(0x2028);
const PARAGRAPH_SEPARATOR = String.fromCharCode(0x2029);

export function jsonLdScript(data: unknown): string {
  return JSON.stringify(data)
    .replace(/</g, "\\u003c")
    .replace(/>/g, "\\u003e")
    .replace(/&/g, "\\u0026")
    .replaceAll(LINE_SEPARATOR, "\\u2028")
    .replaceAll(PARAGRAPH_SEPARATOR, "\\u2029");
}

// ------------------------------------------------------------------ job text

export type Block = { kind: "heading"; text: string } | { kind: "paragraph"; text: string } | { kind: "list"; items: string[] };

/**
 * Plain job text as blocks: lines starting with `- ` become list items; a short line
 * without closing punctuation that is not the last line becomes a section heading.
 */
export function descriptionBlocks(text: string): Block[] {
  const lines = text.replace(/\r\n?/g, "\n").split("\n");
  const blocks: Block[] = [];
  let list: string[] | null = null;
  lines.forEach((raw, index) => {
    const line = raw.trim();
    if (line.startsWith("- ")) {
      if (!list) {
        list = [];
        blocks.push({ kind: "list", items: list });
      }
      list.push(line.slice(2).trim());
      return;
    }
    list = null;
    if (!line) return;
    const looksLikeHeading = line.length <= 60 && !/[.!?؟:،,]$/.test(line) && index < lines.length - 1;
    blocks.push(looksLikeHeading ? { kind: "heading", text: line } : { kind: "paragraph", text: line });
  });
  return blocks;
}

export function localTitle(job: Pick<PublicJob, "title" | "title_ar">, locale: Locale): string {
  return locale === "ar" && job.title_ar.trim() ? job.title_ar : job.title;
}

export function localDescription(job: Pick<PublicJobDetail, "description_en" | "description_ar">, locale: Locale): string {
  return locale === "ar" && job.description_ar.trim() ? job.description_ar : job.description_en;
}

/** "AED 30,000 to 38,000 a month" style text, or null when the job hides its salary. */
export function salaryRange(salary: Salary | null | undefined, locale: Locale): { min: string; max: string; currency: string } | null {
  if (!salary || !salary.currency) return null;
  const format = new Intl.NumberFormat(locale === "ar" ? "ar-AE" : "en-AE", { maximumFractionDigits: 0 });
  const min = Number(salary.min);
  const max = Number(salary.max);
  if (!Number.isFinite(min) || !Number.isFinite(max)) return null;
  return { min: format.format(min), max: format.format(max), currency: salary.currency };
}

// ------------------------------------------------------------------ the public form

export type FieldMode = "required" | "optional" | "hidden";

export interface PublicOption {
  id: string;
  label: string;
  label_ar: string;
}

export interface PublicQuestion {
  id: string;
  kind: "text" | "choice" | "yes_no";
  label: string;
  label_ar: string;
  required: boolean;
  max_length: number;
  options: PublicOption[];
}

export interface PublicForm {
  standard: { phone: FieldMode; location: FieldMode; profile_url: FieldMode };
  questions: PublicQuestion[];
  knockout_questions: Array<{ id: string; question: string; question_ar: string }>;
  files: Array<{ id: string; label: string; label_ar: string; required: boolean }>;
  self_identification: Array<{ id: string; label: string; label_ar: string; options: PublicOption[] }>;
}

const ID = /^[a-z][a-z0-9_]{0,47}$/;

function str(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function mode(value: unknown): FieldMode {
  return value === "required" || value === "hidden" ? value : "optional";
}

function options(value: unknown): PublicOption[] {
  if (!Array.isArray(value)) return [];
  return value
    .map((o) => o as Record<string, unknown>)
    .filter((o) => ID.test(str(o.id)))
    .map((o) => ({ id: str(o.id), label: str(o.label), label_ar: str(o.label_ar) }));
}

function list(value: unknown): Array<Record<string, unknown>> {
  return Array.isArray(value) ? value.filter((v): v is Record<string, unknown> => !!v && typeof v === "object") : [];
}

/** The API's public form, with anything malformed dropped (never trusted blindly). */
export function readPublicForm(raw: unknown): PublicForm {
  const form = (raw && typeof raw === "object" ? raw : {}) as Record<string, unknown>;
  const standard = (form.standard && typeof form.standard === "object" ? form.standard : {}) as Record<string, unknown>;
  return {
    standard: { phone: mode(standard.phone), location: mode(standard.location), profile_url: mode(standard.profile_url) },
    questions: list(form.questions)
      .filter((q) => ID.test(str(q.id)) && ["text", "choice", "yes_no"].includes(str(q.kind)))
      .map((q) => ({
        id: str(q.id),
        kind: str(q.kind) as PublicQuestion["kind"],
        label: str(q.label),
        label_ar: str(q.label_ar),
        required: q.required === true,
        max_length: typeof q.max_length === "number" ? q.max_length : 1000,
        options: options(q.options),
      })),
    knockout_questions: list(form.knockout_questions)
      .filter((k) => ID.test(str(k.id)))
      .map((k) => ({ id: str(k.id), question: str(k.question), question_ar: str(k.question_ar) })),
    files: list(form.files)
      .filter((f) => ID.test(str(f.id)))
      .map((f) => ({ id: str(f.id), label: str(f.label), label_ar: str(f.label_ar), required: f.required === true })),
    self_identification: list(form.self_identification)
      .filter((c) => ID.test(str(c.id)))
      .map((c) => ({ id: str(c.id), label: str(c.label), label_ar: str(c.label_ar), options: options(c.options) })),
  };
}

export function pick(locale: Locale, en: string, ar: string): string {
  return locale === "ar" && ar.trim() ? ar : en;
}

// ------------------------------------------------------------------ client address

const IPV4 = /^(\d{1,3}\.){3}\d{1,3}$/;
const IPV6 = /^[0-9a-fA-F:]{2,45}$/;

function validIp(value: string | null | undefined): string | null {
  const ip = (value ?? "").trim();
  if (IPV4.test(ip) && ip.split(".").every((p) => Number(p) <= 255)) return ip;
  if (ip.includes(":") && IPV6.test(ip)) return ip.toLowerCase();
  return null;
}

/**
 * The visitor's address as the reverse proxy in front of the web app saw it: `X-Real-IP`,
 * else the last `X-Forwarded-For` entry (the one our own proxy appended; earlier entries
 * are whatever the client claimed). Null when there is none: the API then counts the
 * request against the web server's own address.
 */
export function clientIpFrom(headers: Pick<Headers, "get">): string | null {
  const real = validIp(headers.get("x-real-ip"));
  if (real) return real;
  const forwarded = (headers.get("x-forwarded-for") ?? "").split(",").map((p) => p.trim()).filter(Boolean);
  return validIp(forwarded.at(-1));
}

/** The header the API reads the visitor's address from (trusted from the web server only). */
export const CLIENT_IP_HEADER = "X-Pairly-Client-IP";

const SLUG = /^[a-z0-9][a-z0-9-]{1,79}$/;

export function isSlug(value: string): boolean {
  return SLUG.test(value);
}
