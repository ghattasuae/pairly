// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology

/**
 * Hiring manager shortlist review (brief 5.3): the pure parts, shared by the server page
 * and the client screen, so they can be tested without a browser.
 *
 * Every shortlisted candidate of one job on one screen: a short summary from the parsed
 * CV, the rubric scores, the strongest evidence quotes and the interview status, then a
 * decision per candidate. The decisions reuse what the pipeline already audits:
 *
 * - advance: a stage move to the next stage (the interview stage by default);
 * - hold: a note with the reason and the "On hold" tag (no new stage, reversible);
 * - reject: a stage move to rejected with the reason, which drafts the rejection, then
 *   the draft is submitted to the approval queue. Nothing reaches the candidate until a
 *   different person approves it.
 */
import type { HireCard, HireProfile } from "./types";
import type { Interview } from "./scheduling";
import type { ScreeningPanel } from "./screening";

export const SHORTLIST_STAGE = "shortlist";
export const DECISIONS = ["advance", "hold", "reject"] as const;
export type Decision = (typeof DECISIONS)[number];

/** Reasons are required for hold and reject; the API seals at most 500 characters. */
export const REASON_MIN = 5;
export const REASON_MAX = 500;

export interface ShortlistCandidate {
  card: HireCard;
  profile: HireProfile | null;
  screening: ScreeningPanel | null;
  interviews: Interview[];
}

export interface Highlight {
  criterion: string;
  quote: string;
}

export interface Summary {
  role: string | null;
  employer: string | null;
  skills: string[];
}

export type ReasonProblem = "reasonRequired" | "reasonShort" | "reasonLong" | null;

export function needsReason(decision: Decision): boolean {
  return decision === "hold" || decision === "reject";
}

/** Why a decision cannot be recorded yet, or null. */
export function reasonProblem(decision: Decision, reason: string): ReasonProblem {
  const text = reason.trim();
  if (text.length > REASON_MAX) return "reasonLong";
  if (!needsReason(decision)) return null;
  if (text.length === 0) return "reasonRequired";
  if (text.length < REASON_MIN) return "reasonShort";
  return null;
}

function value(item: unknown): string | null {
  if (item && typeof item === "object" && "value" in item) {
    const v = (item as { value: unknown }).value;
    return typeof v === "string" && v.trim() ? v.trim() : null;
  }
  return null;
}

/** The latest role and up to `limit` skills from the parsed CV (evidenced values only). */
export function summarize(screening: ScreeningPanel | null, limit = 6): Summary {
  const profile = screening?.profile;
  if (!profile) return { role: null, employer: null, skills: [] };
  const experience = Array.isArray(profile.experience) ? (profile.experience as unknown[]) : [];
  const latest = experience[0] as { title?: unknown; employer?: unknown } | undefined;
  const skills = Array.isArray(profile.skills)
    ? [...new Set((profile.skills as unknown[]).map(value).filter((s): s is string => s !== null))].slice(0, limit)
    : [];
  return { role: value(latest?.title), employer: value(latest?.employer), skills };
}

/**
 * The strongest evidence: one quote per criterion, highest scored criteria first (must
 * haves before nice to haves on a tie), at most `limit`. From the full screening when
 * there is one, else from the pipeline profile's screening summary.
 */
export function highlights(candidate: Pick<ShortlistCandidate, "profile" | "screening">, limit = 3): Highlight[] {
  const screening = candidate.screening;
  if (screening && screening.criteria.length > 0) {
    return [...screening.criteria]
      .filter((c) => c.evidence.some((e) => e.quote.trim()))
      .sort((a, b) => b.score - a.score || Number(b.must_have) - Number(a.must_have) || b.weight - a.weight)
      .slice(0, limit)
      .map((c) => ({ criterion: c.name, quote: c.evidence.find((e) => e.quote.trim())!.quote.trim() }));
  }
  const criteria = candidate.profile?.screening?.criteria ?? [];
  const rank: Record<string, number> = { met: 0, partly_met: 1, not_met: 2, unknown: 3 };
  return [...criteria]
    .filter((c) => c.quotes.some((q) => q.trim()))
    .sort((a, b) => (rank[a.result] ?? 4) - (rank[b.result] ?? 4))
    .slice(0, limit)
    .map((c) => ({ criterion: c.criterion, quote: c.quotes.find((q) => q.trim())!.trim() }));
}

const STATUS_ORDER = ["booked", "rescheduled", "proposed", "completed", "no_show", "cancelled"];

/** The interview that says most about where the candidate is: open ones first, then the latest. */
export function currentInterview(interviews: readonly Interview[]): Interview | null {
  if (interviews.length === 0) return null;
  return [...interviews].sort((a, b) => {
    const order = STATUS_ORDER.indexOf(a.status) - STATUS_ORDER.indexOf(b.status);
    if (order !== 0) return order;
    return (b.starts_at ?? b.created_at).localeCompare(a.starts_at ?? a.created_at);
  })[0]!;
}

/** Group a job's interviews by application. */
export function interviewsByApplication(interviews: readonly Interview[]): Map<string, Interview[]> {
  const out = new Map<string, Interview[]>();
  for (const interview of interviews) {
    const list = out.get(interview.application_id) ?? [];
    list.push(interview);
    out.set(interview.application_id, list);
  }
  return out;
}

/** The decision a key press picks, when the focus is not in a text field. */
export function decisionForKey(key: string): Decision | null {
  switch (key.toLowerCase()) {
    case "a":
      return "advance";
    case "h":
      return "hold";
    case "r":
      return "reject";
    default:
      return null;
  }
}
