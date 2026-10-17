// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology

/**
 * Recruiter screening mode (brief 5.3, WP2.6): the pure parts, shared by the server page
 * and the client screen, so they can be tested without a browser.
 *
 * - The URL keeps the open candidate and the search (`?candidate=<id>&q=`), so a reload
 *   or a shared link opens exactly where the person was.
 * - The queue order is the API's (oldest application first) and never changes after an
 *   action: the position is kept (J/K move from where you are).
 * - Prefetch: the next two candidates and the previous one load in the background, so
 *   J shows the next one at once.
 * - "Why not higher" and flags: one plain sentence each, from a translation key and the
 *   rubric's own anchor text (never the model's prose alone).
 */
import type { components } from "@pairly/shared-ts";

import type { HireProfile } from "./types";

type S = components["schemas"];

export type ScreeningQueue = S["HireScreeningQueueOut"];
export type QueueItem = S["HireQueueItemOut"];
export type ScreeningPanel = S["HireScreeningOut"];
export type ScreeningCriterion = S["HireScreeningCriterion"];
export type ScreeningEvidence = S["HireScreeningEvidence"];
export type ScreeningFlag = S["HireScreeningFlag"];

/** Everything the screen shows for one candidate. `screening` is null before a screening. */
export interface CandidateDetail {
  screening: ScreeningPanel | null;
  profile: HireProfile;
}

export interface ScreenState {
  candidate: string | null;
  q: string;
}

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

function first(value: string | string[] | undefined): string | undefined {
  return Array.isArray(value) ? value[0] : value;
}

export function parseScreenState(query: Record<string, string | string[] | undefined>): ScreenState {
  const candidate = first(query.candidate) ?? null;
  return {
    candidate: candidate && UUID_RE.test(candidate) ? candidate : null,
    q: (first(query.q) ?? "").slice(0, 100),
  };
}

export function screenQuery(state: ScreenState): string {
  const params = new URLSearchParams();
  if (state.candidate) params.set("candidate", state.candidate);
  if (state.q) params.set("q", state.q);
  const text = params.toString();
  return text ? `?${text}` : "";
}

/** The queue items matching the search (label, reference or tag), in queue order. */
export function filterQueue(items: readonly QueueItem[], q: string): QueueItem[] {
  const needle = q.trim().toLowerCase();
  if (!needle) return [...items];
  return items.filter((item) =>
    [item.label, item.reference, ...item.tags].some((v) => v.toLowerCase().includes(needle)),
  );
}

/** The candidate `delta` places from `current` (clamped to the queue), or null. */
export function neighbour(items: readonly QueueItem[], current: string | null, delta: number): string | null {
  if (items.length === 0) return null;
  const index = items.findIndex((i) => i.application_id === current);
  if (index < 0) return items[delta < 0 ? items.length - 1 : 0]?.application_id ?? null;
  const next = Math.min(items.length - 1, Math.max(0, index + delta));
  return items[next]?.application_id ?? null;
}

/** The candidates to load ahead of time: the next two and the previous one. */
export function prefetchIds(items: readonly QueueItem[], current: string | null): string[] {
  const index = items.findIndex((i) => i.application_id === current);
  if (index < 0) return [];
  return [index + 1, index + 2, index - 1]
    .filter((i) => i >= 0 && i < items.length)
    .map((i) => items[i]!.application_id);
}

/** 1-based position of `current` in the queue, or 0. */
export function positionOf(items: readonly QueueItem[], current: string | null): number {
  return items.findIndex((i) => i.application_id === current) + 1;
}

export const MAX_COMPARE = 3;

/** Add or remove one candidate from the comparison (at most three; the oldest drops). */
export function toggleCompare(selection: readonly string[], id: string): string[] {
  if (selection.includes(id)) return selection.filter((s) => s !== id);
  const next = [...selection, id];
  return next.length > MAX_COMPARE ? next.slice(next.length - MAX_COMPARE) : next;
}

/** A small cache of candidate details (most recent last), so J/K never wait twice. */
export class DetailCache {
  private readonly entries = new Map<string, CandidateDetail>();

  constructor(private readonly capacity = 40) {}

  get(id: string): CandidateDetail | undefined {
    const found = this.entries.get(id);
    if (found) {
      this.entries.delete(id);
      this.entries.set(id, found);
    }
    return found;
  }

  has(id: string): boolean {
    return this.entries.has(id);
  }

  /** Read without counting as a use (safe while rendering). */
  peek(id: string): CandidateDetail | undefined {
    return this.entries.get(id);
  }

  set(id: string, detail: CandidateDetail): void {
    this.entries.delete(id);
    this.entries.set(id, detail);
    while (this.entries.size > this.capacity) {
      const oldest = this.entries.keys().next().value;
      if (oldest === undefined) break;
      this.entries.delete(oldest);
    }
  }

  delete(id: string): void {
    this.entries.delete(id);
  }
}

// ------------------------------------------------------------------ explanations

export type WhyKey = "notScored" | "top" | "noEvidence" | "nextLevel" | "generic";

/** Which sentence explains why a criterion did not score higher, and its values. */
export function whyNotHigher(
  criterion: Pick<ScreeningCriterion, "score" | "flags" | "evidence" | "next_anchor">,
  max: number,
): { key: WhyKey; values: Record<string, string | number> } {
  if (criterion.flags.includes("not_scored")) return { key: "notScored", values: {} };
  if (criterion.score >= max) return { key: "top", values: { max } };
  if (criterion.evidence.length === 0) return { key: "noEvidence", values: {} };
  if (criterion.next_anchor) {
    return { key: "nextLevel", values: { score: criterion.score, max, next: criterion.next_anchor } };
  }
  return { key: "generic", values: { score: criterion.score, max } };
}

/** Flag codes with their own sentence in the messages (`Screen.flags.<code>`). */
export const KNOWN_FLAGS = new Set([
  "end_before_start",
  "future_date",
  "overlapping_roles",
  "unexplained_gap",
  "title_inflation",
  "unsupported_skills",
  "boilerplate",
  "mass_application",
]);

/** Criterion flags with their own sentence (`Screen.criterionFlags.<flag>`). */
export const KNOWN_CRITERION_FLAGS = new Set(["no_evidence", "evidence_rejected", "low_confidence", "not_scored"]);

export function flagKey(code: string): string {
  return KNOWN_FLAGS.has(code) ? code : "unknown";
}

export function criterionFlagKeys(flags: readonly string[]): string[] {
  return [...new Set(flags.map((f) => (f.startsWith("why_not_higher_dropped") ? "why_dropped" : f)))].filter(
    (f) => KNOWN_CRITERION_FLAGS.has(f) || f === "why_dropped",
  );
}

// ------------------------------------------------------------------ blind text

export interface TextPiece {
  text: string;
  /** Evidence ids that cover this piece (for marks). */
  evidence: string[];
  /** A blind placeholder such as `[name]`. */
  hidden: boolean;
}

export interface TextRange {
  id: string;
  start: number;
  end: number;
}

const PLACEHOLDER = String.raw`\[(?:name|contact|gender|age|nationality|marital status|address|university|religion)\]`;
const PLACEHOLDER_RE = new RegExp(PLACEHOLDER, "g");
const PLACEHOLDER_ONLY = new RegExp(`^${PLACEHOLDER}$`);

/**
 * Split the screening text into pieces: plain text, evidence marks (by display offsets)
 * and blind placeholders. Ranges outside the text or overlapping each other are kept
 * as far as they fit; the text is never changed.
 */
export function splitText(text: string, ranges: readonly TextRange[]): TextPiece[] {
  const cuts = new Set<number>([0, text.length]);
  const valid = ranges.filter((r) => r.start >= 0 && r.end <= text.length && r.end > r.start);
  for (const r of valid) {
    cuts.add(r.start);
    cuts.add(r.end);
  }
  for (const m of text.matchAll(PLACEHOLDER_RE)) {
    cuts.add(m.index);
    cuts.add(m.index + m[0].length);
  }
  const points = [...cuts].sort((a, b) => a - b);
  const pieces: TextPiece[] = [];
  for (let i = 0; i < points.length - 1; i += 1) {
    const start = points[i]!;
    const end = points[i + 1]!;
    if (end <= start) continue;
    const piece = text.slice(start, end);
    pieces.push({
      text: piece,
      evidence: valid.filter((r) => r.start <= start && r.end >= end).map((r) => r.id),
      hidden: PLACEHOLDER_ONLY.test(piece),
    });
  }
  return pieces;
}

/** Evidence ranges of a screening, by display offsets (blind) or original offsets. */
export function evidenceRanges(screening: ScreeningPanel): TextRange[] {
  const out: TextRange[] = [];
  screening.criteria.forEach((c) => {
    c.evidence.forEach((e, index) => {
      const start = screening.blind ? e.display_start : e.start;
      const end = screening.blind ? e.display_end : e.end;
      if (typeof start === "number" && typeof end === "number") {
        out.push({ id: evidenceId(c.criterion_id, index), start, end });
      }
    });
  });
  return out;
}

export function evidenceId(criterionId: string, index: number): string {
  return `ev-${criterionId}-${index}`;
}

/** The tag that holds a candidate (H): reversible, visible on the board, no new stage. */
export const HOLD_TAG = "On hold";

export function withTag(tags: readonly string[], tag: string): string[] {
  return tags.some((t) => t.toLowerCase() === tag.toLowerCase()) ? [...tags] : [...tags, tag];
}
