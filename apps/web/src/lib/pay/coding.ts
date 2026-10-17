// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology

/**
 * GL coding panel state (brief 4.5.3), pure. A line's draft starts from its current codes;
 * a suggestion is only applied when the person asks (one line, or every high confidence
 * line still uncoded). Only lines whose codes changed are sent.
 */
import type { PayCodingSuggestion, PayLineCoding } from "./types";

export interface LineDraft {
  gl_code: string;
  cost_centre: string;
}

export type Drafts = Record<number, LineDraft>;

export interface LineChange {
  line_index: number;
  gl_code: string | null;
  cost_centre: string | null;
}

/** The drafts of every line from its current codes ("" when uncoded). */
export function draftsOf(lines: readonly PayLineCoding[]): Drafts {
  return Object.fromEntries(
    lines.map((line) => [line.line_index, { gl_code: line.gl_code ?? "", cost_centre: line.cost_centre ?? "" }]),
  );
}

/** A draft with the suggestion's codes (a field the suggestion leaves empty is kept). */
export function applySuggestion(draft: LineDraft, suggestion: PayCodingSuggestion | null | undefined): LineDraft {
  if (!suggestion) return draft;
  return {
    gl_code: suggestion.gl_code ?? draft.gl_code,
    cost_centre: suggestion.cost_centre ?? draft.cost_centre,
  };
}

/** Every uncoded line with a high confidence suggestion takes it; the rest stay as they are. */
export function acceptHigh(lines: readonly PayLineCoding[], drafts: Drafts): Drafts {
  const next: Drafts = { ...drafts };
  for (const line of lines) {
    const draft = next[line.line_index] ?? { gl_code: "", cost_centre: "" };
    if (line.suggestion?.level === "high" && !draft.gl_code) {
      next[line.line_index] = applySuggestion(draft, line.suggestion);
    }
  }
  return next;
}

/** The lines whose codes differ from what is stored, as the API takes them. */
export function changesOf(lines: readonly PayLineCoding[], drafts: Drafts): LineChange[] {
  const out: LineChange[] = [];
  for (const line of lines) {
    const draft = drafts[line.line_index];
    if (!draft) continue;
    const gl = draft.gl_code || null;
    const cc = draft.cost_centre || null;
    if (gl !== (line.gl_code ?? null) || cc !== (line.cost_centre ?? null)) {
      out.push({ line_index: line.line_index, gl_code: gl, cost_centre: cc });
    }
  }
  return out;
}

/** The confidence as a whole percentage (0 to 100). */
export function percent(confidence: number): number {
  return Math.max(0, Math.min(100, Math.round(confidence * 100)));
}
