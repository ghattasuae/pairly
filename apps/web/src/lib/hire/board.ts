// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology

/**
 * The pipeline board's state that lives in the URL (brief 5.3: never lose your place):
 * the filters, the open candidate and the scroll position. Pure functions, so the
 * server page and the client board read and write the same query string.
 */
import type { HireCard, HireStage } from "./types";

export const FLAGS = ["needs_review", "waiting", "referred"] as const;
export type BoardFlag = (typeof FLAGS)[number];

export interface BoardState {
  q: string;
  tag: string | null;
  flag: BoardFlag | null;
  candidate: string | null;
  /** Horizontal scroll of the board, in pixels. */
  x: number;
  /** Vertical scroll of the page, in pixels. */
  y: number;
}

export const EMPTY_STATE: BoardState = { q: "", tag: null, flag: null, candidate: null, x: 0, y: 0 };

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const TAG_RE = /^[\p{L}\p{N}][\p{L}\p{N} \-./+#]{0,31}$/u;

function first(value: string | string[] | undefined): string | undefined {
  return Array.isArray(value) ? value[0] : value;
}

function position(value: string | undefined): number {
  const n = Number.parseInt(value ?? "", 10);
  return Number.isFinite(n) && n >= 0 && n < 1_000_000 ? n : 0;
}

/** Parse the query string (server `searchParams` or `URLSearchParams`). Never throws. */
export function parseBoardState(query: Record<string, string | string[] | undefined>): BoardState {
  const q = (first(query.q) ?? "").slice(0, 100);
  const tag = first(query.tag) ?? null;
  const flag = first(query.flag) ?? null;
  const candidate = first(query.candidate) ?? null;
  return {
    q,
    tag: tag && TAG_RE.test(tag) ? tag : null,
    flag: flag && (FLAGS as readonly string[]).includes(flag) ? (flag as BoardFlag) : null,
    candidate: candidate && UUID_RE.test(candidate) ? candidate : null,
    x: position(first(query.x)),
    y: position(first(query.y)),
  };
}

export function boardQuery(state: BoardState): string {
  const params = new URLSearchParams();
  if (state.q) params.set("q", state.q);
  if (state.tag) params.set("tag", state.tag);
  if (state.flag) params.set("flag", state.flag);
  if (state.candidate) params.set("candidate", state.candidate);
  if (state.x > 0) params.set("x", String(Math.round(state.x)));
  if (state.y > 0) params.set("y", String(Math.round(state.y)));
  const text = params.toString();
  return text ? `?${text}` : "";
}

/** The cards that match the filters, in board order. */
export function filterCards(cards: HireCard[], state: Pick<BoardState, "q" | "tag" | "flag">): HireCard[] {
  const needle = state.q.trim().toLowerCase();
  return cards.filter((card) => {
    if (state.tag && !card.tags.some((t) => t.toLowerCase() === state.tag?.toLowerCase())) return false;
    if (state.flag === "needs_review" && !card.needs_human_review) return false;
    if (state.flag === "waiting" && !card.sla_overdue) return false;
    if (state.flag === "referred" && !card.referred) return false;
    if (!needle) return true;
    return [card.label, card.reference, ...card.tags].some((v) => v.toLowerCase().includes(needle));
  });
}

/** Cards grouped by stage, every stage present (empty columns stay visible). */
export function columns(stages: HireStage[], cards: HireCard[]): Map<string, HireCard[]> {
  const grouped = new Map<string, HireCard[]>(stages.map((s) => [s.key, []]));
  for (const card of cards) {
    const list = grouped.get(card.stage_key) ?? grouped.get(stages[0]?.key ?? "applied");
    list?.push(card);
  }
  return grouped;
}

/** The next active stage, or "hired" after the last one (mirrors the API's advance). */
export function nextStage(stages: HireStage[], key: string): HireStage | null {
  const active = stages.filter((s) => s.kind === "active");
  const index = active.findIndex((s) => s.key === key);
  if (index < 0) return null;
  return active[index + 1] ?? stages.find((s) => s.kind === "hired") ?? null;
}

/** Every tag on the board, for the filter. */
export function allTags(cards: HireCard[]): string[] {
  return [...new Set(cards.flatMap((c) => c.tags))].sort((a, b) => a.localeCompare(b));
}

/** A fresh Idempotency-Key for one user action. */
export function actionKey(): string {
  return `hire-${crypto.randomUUID()}`;
}
