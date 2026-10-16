// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
"use client";

import { useTranslations } from "next-intl";
import { useCallback } from "react";

import type { Subject } from "@/lib/evidence";

const MAX_EVIDENCE = 80;
/**
 * Code point ranges never shown as they are: control characters, the soft hyphen,
 * zero-width and bidi controls, word joiners, the byte order mark, tag characters.
 */
const INVISIBLE_RANGES: readonly (readonly [number, number])[] = [
  [0x0000, 0x0008],
  [0x000b, 0x000c],
  [0x000e, 0x001f],
  [0x007f, 0x009f],
  [0x00ad, 0x00ad],
  [0x200b, 0x200f],
  [0x202a, 0x202e],
  [0x2060, 0x2064],
  [0x2066, 0x2069],
  [0xfeff, 0xfeff],
  [0xe0000, 0xe007f],
];

function visible(char: string): boolean {
  const cp = char.codePointAt(0) ?? 0;
  return !INVISIBLE_RANGES.some(([from, to]) => cp >= from && cp <= to);
}

/**
 * A short, clean excerpt of what a guardrail found, as the API's `clean_evidence` does:
 * invisible characters removed, double quotes made single, whitespace collapsed, cut at
 * 80 characters. It comes from an untrusted document and is only ever rendered as text.
 */
export function cleanEvidence(evidence: string, limit = MAX_EVIDENCE): string {
  const stripped = Array.from(evidence).filter(visible).join("");
  const text = stripped.replaceAll('"', "'").split(/\s+/).filter(Boolean).join(" ");
  return text.length > limit ? `${text.slice(0, limit - 3).trimEnd()}...` : text;
}

/** Explanation keys the API sends look like `guardrails.hidden_text.tiny_font` or `risk.amount.outlier`. */
export function isExplanationKey(key: unknown): key is string {
  return typeof key === "string" && /^[a-z][a-z0-9_]*(\.[a-z0-9_]+){1,4}$/.test(key);
}

/**
 * One plain sentence for an AI flag (brief 5.3), from the API's explanation keys:
 * guardrail findings (`guardrails.<type>.<code>`) and risk signals (`risk.*`), in the
 * page's language. Unknown keys fall back to a general sentence, never to the raw key.
 */
export function useExplanation() {
  const t = useTranslations("Explain");
  return useCallback(
    (key: string, options: { evidence?: string; subject?: Subject } = {}) => {
      const subject = t(`subjects.${options.subject ?? "document"}` as "subjects.document");
      const evidence = cleanEvidence(options.evidence ?? "");
      const values = { subject, evidence };
      if (isExplanationKey(key) && t.has(key as "risk.amount.outlier")) {
        return t(key as "risk.amount.outlier", values);
      }
      if (key.startsWith("guardrails.")) return t("fallback.guardrail", values);
      return t("fallback.risk");
    },
    [t],
  );
}
