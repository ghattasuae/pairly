// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology

/**
 * Highlights for the document viewer, built from what the API returns. Pure, no I/O.
 *
 * Sources:
 * - extraction fields: `spans[]` with `page` and `bboxes[]` (one box per line) and the
 *   exact `quote`;
 * - guardrail findings: `type`, `code`, `severity`, `page`, `bbox` and `evidence`;
 * - an approval's `context`, by convention (workflow-agnostic, see apps/web/README.md):
 *   `document_id`, `evidence[]` (`label` or `field`, `quote`, `page`, `bboxes` or `bbox`),
 *   `guardrail_findings[]` (as above), `decision_id` or `decision_ids[]`.
 *
 * Everything is checked here: a box needs four finite numbers, a page is a positive
 * integer. Anything malformed is dropped, never shown in the wrong place.
 */
import type { BBox } from "./api/types";

export type HighlightKind = "evidence" | "guardrail";

export interface Highlight {
  id: string;
  kind: HighlightKind;
  /** 1-based page, or null when the document has no layout. */
  page: number | null;
  boxes: BBox[];
  /** Field name or finding title. */
  label: string;
  /** The exact text (evidence quote or guardrail excerpt). */
  quote: string;
  /** For guardrail findings: `guardrails.<type>.<code>`. */
  explanationKey?: string;
  severity?: string;
}

type Json = Record<string, unknown>;

const isObject = (value: unknown): value is Json => typeof value === "object" && value !== null && !Array.isArray(value);
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const DECISION = /^dec_[0-9A-HJKMNP-TV-Z]{26}$/;

export function toBox(value: unknown): BBox | null {
  if (!isObject(value)) return null;
  const { x0, top, x1, bottom } = value;
  const nums = [x0, top, x1, bottom];
  if (!nums.every((n) => typeof n === "number" && Number.isFinite(n))) return null;
  const box = { x0: x0 as number, top: top as number, x1: x1 as number, bottom: bottom as number };
  return box.x1 > box.x0 && box.bottom > box.top ? box : null;
}

function toPage(value: unknown): number | null {
  return typeof value === "number" && Number.isInteger(value) && value >= 1 && value <= 100_000 ? value : null;
}

function text(value: unknown, max = 500): string {
  return typeof value === "string" ? value.slice(0, max) : "";
}

function boxes(item: Json): BBox[] {
  const list = Array.isArray(item.bboxes) ? item.bboxes : item.bbox ? [item.bbox] : [];
  return list.map(toBox).filter((b): b is BBox => b !== null).slice(0, 50);
}

/** One highlight per located span of every extracted field. */
export function highlightsFromFields(fields: readonly { path: string; spans: readonly unknown[] }[]): Highlight[] {
  const out: Highlight[] = [];
  for (const field of fields) {
    field.spans.forEach((span, index) => {
      if (!isObject(span)) return;
      out.push({
        id: `field:${field.path}:${index}`,
        kind: "evidence",
        page: toPage(span.page),
        boxes: boxes(span),
        label: field.path,
        quote: text(span.quote),
      });
    });
  }
  return out;
}

/** One highlight per guardrail finding (a finding without a box still lists in the legend). */
export function highlightsFromFindings(findings: unknown): Highlight[] {
  if (!Array.isArray(findings)) return [];
  return findings.filter(isObject).slice(0, 200).map((f, index) => {
    const type = text(f.type, 64);
    const code = text(f.code, 64);
    return {
      id: `finding:${text(f.id, 64) || index}`,
      kind: "guardrail" as const,
      page: toPage(f.page),
      boxes: boxes(f),
      label: code || type,
      quote: text(f.evidence),
      ...(type && code ? { explanationKey: `guardrails.${type}.${code}` } : {}),
      ...(typeof f.severity === "string" ? { severity: f.severity } : {}),
    };
  });
}

/** Highlights, the document and decision records named in an approval's context. */
export function readContext(context: unknown): {
  documentId: string | null;
  highlights: Highlight[];
  decisionIds: string[];
} {
  if (!isObject(context)) return { documentId: null, highlights: [], decisionIds: [] };
  const documentId = typeof context.document_id === "string" && UUID.test(context.document_id) ? context.document_id : null;
  const evidence = Array.isArray(context.evidence) ? context.evidence.filter(isObject).slice(0, 200) : [];
  const highlights: Highlight[] = evidence.map((item, index) => ({
    id: `evidence:${index}`,
    kind: "evidence",
    page: toPage(item.page),
    boxes: boxes(item),
    label: text(item.label, 120) || text(item.field, 120),
    quote: text(item.quote),
  }));
  highlights.push(...highlightsFromFindings(context.guardrail_findings));
  const ids = [context.decision_id, ...(Array.isArray(context.decision_ids) ? context.decision_ids : [])];
  const decisionIds = [...new Set(ids.filter((id): id is string => typeof id === "string" && DECISION.test(id)))];
  return { documentId, highlights, decisionIds };
}
