// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology

/**
 * The vendor portal's shapes and small pure helpers (brief 4.7, 5.1 item 11), shared by
 * the pages, the server actions and the tests. The API decides every status and date;
 * this only orders the steps for display.
 */
import type { components } from "@pairly/shared-ts";

type S = components["schemas"];

export type VendorPortalMe = S["PayPortalMeOut"];
export type VendorPortalInvoice = S["PayPortalInvoiceOut"];
export type VendorPortalInvoiceDetail = S["PayPortalInvoiceDetailOut"];
export type VendorPortalMessage = S["PayPortalMessageOut"];
export type VendorPortalDecision = S["PayPortalDecisionOut"];
export type VendorPortalDetails = S["PayPortalDetailsOut"];
export type VendorPortalRequest = S["PayPortalRequestOut"];
export type VendorPortalContact = S["PayPortalContactOut"];

export const INVOICE_STEPS = ["received", "in_review", "approved", "scheduled", "paid"] as const;
export const CREDIT_STEPS = ["received", "in_review", "applied"] as const;

export interface StepView {
  step: string;
  state: "done" | "current" | "ahead";
  at: string | null;
}

/** Every step with its date: reached ones are done, the last reached is current. */
export function stepViews(invoice: Pick<VendorPortalInvoice, "steps" | "status">): StepView[] {
  if (invoice.status === "rejected") {
    return [
      ...invoice.steps.filter((s) => s.at !== null).map((s) => ({ step: s.step, state: "done" as const, at: s.at })),
      { step: "rejected", state: "current", at: null },
    ];
  }
  const last = invoice.steps.reduce((found, s, index) => (s.at !== null ? index : found), -1);
  return invoice.steps.map((s, index) => ({
    step: s.step,
    state: index < last ? "done" : index === last ? "current" : "ahead",
    at: s.at,
  }));
}

const LOGIN_RE = /^vl1\.([0-9a-f]{32})\.[0-9a-f]{32}\.[0-9a-f]{32}\.[0-9a-f]{32}\.[A-Za-z0-9_-]{43}$/;
export const SESSION_RE = /^vs1\.[0-9a-f]{32}\.[0-9a-f]{32}\.[0-9a-f]{32}\.[0-9a-f]{32}\.[A-Za-z0-9_-]{43}$/;
export const ORG_RE = /^[0-9a-f]{32}$/;

export function isVendorLoginToken(value: string): boolean {
  return LOGIN_RE.test(value);
}

/** The organisation id a magic link names (to ask for a new link when it expired). */
export function orgFromVendorToken(value: string): string | null {
  const login = LOGIN_RE.exec(value);
  if (login) return login[1] ?? null;
  return SESSION_RE.test(value) ? (value.split(".")[1] ?? null) : null;
}
