// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology

/**
 * Helpers for the bias and adverse-impact report (M3, brief 3.10): the page's filters and
 * the export link, validated before anything reaches the API, and how each judgement
 * reads.
 */
import { analyticsQuery } from "./analytics";

export interface BiasQuery {
  job_id?: string;
  since?: string;
  until?: string;
  min_sample?: number;
}

/** The page's filters from the URL; anything invalid is dropped. */
export function biasQuery(raw: Record<string, string | string[] | undefined>): BiasQuery {
  const base = analyticsQuery({ job_id: raw.job_id, since: raw.since, until: raw.until });
  const out: BiasQuery = {};
  if (base.job_id) out.job_id = base.job_id;
  if (base.since) out.since = base.since;
  if (base.until) out.until = base.until;
  const min = typeof raw.min_sample === "string" ? Number(raw.min_sample) : NaN;
  if (Number.isInteger(min) && min >= 5 && min <= 1000) out.min_sample = min;
  return out;
}

/** `?job_id=...` for the export, from validated filters only ("" when there are none). */
export function biasExportQuery(params: URLSearchParams): string {
  const raw: Record<string, string> = {};
  params.forEach((value, key) => {
    raw[key] = value;
  });
  const query = biasQuery(raw);
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(query)) search.set(key, String(value));
  const text = search.toString();
  return text ? `?${text}` : "";
}

export type Tone = "default" | "outline" | "signal";

/** Below the four-fifths rule of thumb stands out; everything else stays quiet. */
export function judgementTone(judgement: string): Tone {
  if (judgement === "below_four_fifths") return "signal";
  if (judgement === "reference" || judgement === "meets_four_fifths") return "default";
  return "outline";
}
