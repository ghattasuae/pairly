// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology

/**
 * The candidate portal's shapes and the honest steps of an application (WP2.9). Pure, so
 * the pages and the tests share them. The API decides the step; this only orders the
 * steps for the timeline and says which are done, current or still ahead.
 */
import type { components } from "@pairly/shared-ts";

type S = components["schemas"];

export type PortalMe = S["HirePortalMeOut"];
export type PortalApplication = S["HirePortalApplicationOut"];
export type PortalApplicationDetail = S["HirePortalApplicationDetailOut"];
export type PortalMessage = S["HirePortalMessageOut"];
export type PortalDocumentRequest = S["HirePortalDocumentRequestOut"];
export type PortalJob = S["HirePortalJobOut"];
export type PortalPrivacy = S["HirePortalPrivacyOut"];
export type PortalDecision = S["HirePortalDecisionOut"];
export type PortalAppeal = S["HirePortalAppealOut"];

export const STEPS = ["received", "in_review", "moving_forward", "interview", "offer"] as const;
export const OUTCOMES = ["hired", "not_selected", "withdrawn"] as const;
export type Step = (typeof STEPS)[number] | (typeof OUTCOMES)[number];

export interface StepView {
  step: Step;
  state: "done" | "current" | "ahead";
  at: string | null;
}

function isStep(value: string): value is Step {
  return (STEPS as readonly string[]).includes(value) || (OUTCOMES as readonly string[]).includes(value);
}

/**
 * The path shown to the candidate: every step reached (with its date), the current one,
 * and the usual steps still ahead (none after an outcome).
 */
export function stepViews(app: Pick<PortalApplication, "status" | "history" | "final">): StepView[] {
  const reached = app.history.filter((h) => isStep(h.step));
  const out: StepView[] = reached.map((h, index) => ({
    step: h.step as Step,
    state: index === reached.length - 1 ? "current" : "done",
    at: h.at,
  }));
  if (app.final) return out;
  const last = reached.at(-1)?.step;
  const from = last ? (STEPS as readonly string[]).indexOf(last) : -1;
  for (const step of STEPS.slice(from + 1)) out.push({ step, state: "ahead", at: null });
  return out;
}

/** A stable fingerprint of what the candidate sees, to tell when polling found news. */
export function fingerprint(apps: readonly PortalApplication[]): string {
  return apps.map((a) => `${a.id}:${a.status}:${a.history.length}:${a.open_document_requests}:${a.interviews.length}`).join("|");
}

const TOKEN_RE = /^pl1\.([0-9a-f]{32})\.[0-9a-f]{32}\.[0-9a-f]{32}\.[A-Za-z0-9_-]{43}$/;

export function isLoginToken(value: string): boolean {
  return TOKEN_RE.test(value);
}

/** The organisation id a magic link names (to ask for a new link when it expired). */
export function orgFromToken(value: string): string | null {
  return TOKEN_RE.exec(value)?.[1] ?? null;
}
