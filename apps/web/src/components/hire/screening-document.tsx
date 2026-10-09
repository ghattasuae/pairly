// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
"use client";

import { EyeOff, FileText } from "lucide-react";
import { useTranslations } from "next-intl";
import { useEffect, useMemo, useRef } from "react";

import { DocumentViewer } from "@/components/document/document-viewer";
import { GuardrailNotice } from "@/components/document/guardrail-notice";
import { type Highlight, toBox } from "@/lib/evidence";
import { evidenceId, evidenceRanges, type ScreeningPanel, splitText } from "@/lib/hire/screening";
import { cn } from "@/lib/utils";

/** Evidence quotes and guardrail findings as document viewer highlights. */
export function screeningHighlights(screening: ScreeningPanel): Highlight[] {
  const out: Highlight[] = screening.guardrail_findings.map((f) => ({
    id: `finding:${f.id}`,
    kind: "guardrail" as const,
    page: null,
    boxes: [],
    label: f.code,
    quote: f.evidence,
    explanationKey: `guardrails.${f.type}.${f.code}`,
    severity: f.severity,
    removed: f.removed_from_model_input,
  }));
  screening.criteria.forEach((c) => {
    c.evidence.forEach((e, index) => {
      out.push({
        id: evidenceId(c.criterion_id, index),
        kind: "evidence",
        page: e.page ?? null,
        boxes: e.bboxes.map(toBox).filter((b): b is NonNullable<typeof b> => b !== null),
        label: c.name,
        quote: e.quote,
      });
    });
  });
  return out;
}

function prefersReducedMotion(): boolean {
  return typeof window !== "undefined" && window.matchMedia?.("(prefers-reduced-motion: reduce)").matches === true;
}

/**
 * The left side of screening mode: the CV. While blind, the redacted text the screening
 * keeps for reviewers, with every evidence quote marked and every hidden part labelled;
 * once revealed, the document itself with the evidence boxes drawn on its pages. The
 * guardrail warning comes first either way. Selecting a quote on the right (`active`)
 * scrolls to it here and marks it.
 */
export function ScreeningDocument({
  screening,
  active,
  className,
}: {
  screening: ScreeningPanel | null;
  /** The evidence to show, and a counter so the same quote can be shown again. */
  active: { id: string; seq: number } | null;
  className?: string;
}) {
  const t = useTranslations("Screen");
  const highlights = useMemo(() => (screening ? screeningHighlights(screening) : []), [screening]);
  const textRef = useRef<HTMLDivElement | null>(null);
  const pieces = useMemo(
    () => (screening?.text ? splitText(screening.text, evidenceRanges(screening)) : []),
    [screening],
  );
  const useText = !!screening && (screening.blind || !screening.document_id);

  useEffect(() => {
    if (!active || !useText) return;
    const mark = textRef.current?.querySelector<HTMLElement>(`[data-evidence~="${CSS.escape(active.id)}"]`);
    mark?.scrollIntoView({ block: "center", behavior: prefersReducedMotion() ? "auto" : "smooth" });
  }, [active, useText]);

  if (!screening) {
    return (
      <section className={cn("flex h-full items-center justify-center p-8 text-center", className)}>
        <p className="max-w-sm text-sm text-muted-foreground">{t("notScreened")}</p>
      </section>
    );
  }

  return (
    <section aria-label={t("document")} className={cn("flex min-h-0 flex-col gap-3", className)} data-screen-document>
      <GuardrailNotice highlights={highlights} subject="cv" headingLevel={2} />
      {useText ? (
        screening.text ? (
          <div
            ref={textRef}
            className="min-h-0 flex-1 overflow-y-auto rounded-xl border bg-surface p-5 text-[0.95rem] leading-relaxed whitespace-pre-wrap"
            data-blind-text={screening.blind ? "true" : "false"}
            tabIndex={0}
            aria-label={t("document")}
          >
            {pieces.map((piece, index) => {
              if (piece.hidden) {
                return (
                  <span
                    key={index}
                    className="mx-0.5 inline-flex items-center gap-1 rounded bg-muted px-1 font-sans text-xs text-muted-foreground"
                    title={t("blind.placeholder")}
                  >
                    <EyeOff className="size-3" aria-hidden />
                    {piece.text}
                    <span className="sr-only">{t("blind.placeholder")}</span>
                  </span>
                );
              }
              if (piece.evidence.length === 0) return <span key={index}>{piece.text}</span>;
              const isActive = !!active && piece.evidence.includes(active.id);
              return (
                <mark
                  key={index}
                  data-evidence={piece.evidence.join(" ")}
                  className={cn(
                    "rounded-sm bg-signal-soft/70 px-0.5 text-foreground",
                    isActive && "bg-signal outline-2 outline-offset-1 outline-signal",
                  )}
                >
                  {piece.text}
                </mark>
              );
            })}
          </div>
        ) : (
          <p className="flex items-center gap-2 rounded-xl border bg-surface p-5 text-sm text-muted-foreground">
            <FileText className="size-4" aria-hidden />
            {t("cvUnavailable")}
          </p>
        )
      ) : (
        <DocumentViewer
          documentId={screening.document_id ?? ""}
          highlights={highlights}
          subject="cv"
          layout="stacked"
          focus={active}
          className="min-h-0 flex-1"
        />
      )}
    </section>
  );
}
