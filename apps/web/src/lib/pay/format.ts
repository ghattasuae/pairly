// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology

/**
 * Small, pure helpers for Pay screens. The API validates everything (IBAN checksum,
 * TRN format, phone numbers); these only make input and output easier to read.
 */

/** Countries offered in the vendor forms (the API accepts any ISO 3166 code). */
export const COUNTRIES = ["AE", "SA", "BH", "QA", "KW", "OM", "GB", "DE", "FR", "NL", "US", "IN"] as const;

/** Upper case without spaces, dashes or invisible characters (what the API keeps). */
export function normalizeIban(value: string): string {
  return value.replace(/[\s\-\u00a0\u200b-\u200f\u2060\ufeff]/g, "").toUpperCase();
}

/** Groups of four, the way IBANs are printed and read out on a call. */
export function groupIban(value: string): string {
  const iban = normalizeIban(value);
  return iban.replace(/(.{4})/g, "$1 ").trim();
}

/** A quick shape check before sending (the API does the ISO 13616 checksum). */
export function looksLikeIban(value: string): boolean {
  return /^[A-Z]{2}\d{2}[A-Z0-9]{11,30}$/.test(normalizeIban(value));
}

/** A UAE TRN is 15 digits; spaces and dashes are ignored. */
export function looksLikeTrn(value: string): boolean {
  return /^\d{15}$/.test(value.replace(/[\s-]/g, ""));
}

/** The steps of a bank-detail change, in order, and how far a ticket got. */
export const CHANGE_STEPS = ["requested", "call", "approvals", "cooling", "active"] as const;
export type ChangeStep = (typeof CHANGE_STEPS)[number];

export function changeStepIndex(status: string): number {
  switch (status) {
    case "awaiting_verification":
      return 1;
    case "awaiting_approval":
      return 2;
    case "cooling_off":
      return 3;
    case "completed":
      return 4;
    default:
      return -1;
  }
}

export function isOpenTicket(status: string): boolean {
  return status === "awaiting_verification" || status === "awaiting_approval" || status === "cooling_off";
}
