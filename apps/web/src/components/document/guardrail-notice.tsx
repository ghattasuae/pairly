// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
"use client";

import { ScanSearch, ShieldAlert } from "lucide-react";
import { useTranslations } from "next-intl";
import { useId } from "react";

import { useExplanation } from "@/components/data/explain";
import { Button } from "@/components/ui/button";
import { guardrailHighlights, type Highlight, type Subject } from "@/lib/evidence";
import { cn } from "@/lib/utils";

/**
 * The warning a reviewer sees first when an item has guardrail findings (brief 5.3,
 * 10.1 step 3): one plain sentence per finding from its explanation key, with the
 * flagged text quoted (the highlights come from data only reviewers can read), whether
 * it was left out of what the AI read, and a button that shows it on the page.
 *
 * Not colour alone: a shield icon, a heading that says what it is, a dashed border.
 * Renders nothing without findings.
 */
export function GuardrailNotice({
  highlights,
  subject = "document",
  onShow,
  headingLevel = 3,
  className,
}: {
  highlights: readonly Highlight[];
  subject?: Subject;
  /** Scroll the document viewer to a finding; without it there is no button. */
  onShow?: ((highlight: Highlight) => void) | undefined;
  /** The heading's level where the warning sits (3 in the inbox panel, 2 on a page). */
  headingLevel?: 2 | 3;
  className?: string;
}) {
  const t = useTranslations("Guardrail");
  const subjects = useTranslations("Explain");
  const explain = useExplanation();
  const id = useId();
  const findings = guardrailHighlights(highlights);
  if (findings.length === 0) return null;
  const Heading = headingLevel === 2 ? "h2" : "h3";

  return (
    <section
      aria-labelledby={`${id}-title`}
      data-guardrail-notice
      className={cn("rounded-xl border-2 border-dashed border-destructive/60 bg-destructive/5 p-4 text-sm", className)}
    >
      <Heading id={`${id}-title`} className="flex items-center gap-2 font-semibold text-danger-ink">
        <ShieldAlert className="size-5 shrink-0" aria-hidden />
        {t("title", { count: findings.length })}
      </Heading>
      <p className="mt-1 text-muted-foreground">{t("intro")}</p>
      <ol className="mt-3 space-y-3">
        {findings.map((h, index) => {
          const located = h.page !== null && h.boxes.length > 0;
          return (
            <li key={h.id} data-guardrail-finding={h.id} className="space-y-1.5 border-s-2 border-destructive/50 ps-3">
              <p id={`${id}-${index}`} className="font-medium text-foreground">
                {h.quote
                  ? explain(h.explanationKey ?? "guardrails.unknown.unknown", { evidence: h.quote, subject })
                  : t("withheld", { subject: subjects(`subjects.${subject}`) })}
              </p>
              {h.removed !== undefined && (
                <p data-guardrail-removed={h.removed} className="text-muted-foreground">
                  {h.removed ? t("removed") : t("kept")}
                </p>
              )}
              {onShow && located && (
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  aria-describedby={`${id}-${index}`}
                  onClick={() => onShow(h)}
                >
                  <ScanSearch aria-hidden />
                  {t("show")}
                </Button>
              )}
            </li>
          );
        })}
      </ol>
    </section>
  );
}

/** The inbox row's "Needs a person: guardrail flag", for an item with findings. */
export function GuardrailBadge({ count, className }: { count: number | null | undefined; className?: string }) {
  const t = useTranslations("Guardrail");
  if (!count || count < 1) return null;
  return (
    <span
      data-guardrail-badge
      className={cn(
        "inline-flex items-center gap-1 rounded-full border border-dashed border-destructive/60 bg-destructive/5 px-2 py-0.5 text-xs font-medium text-danger-ink",
        className,
      )}
    >
      <ShieldAlert className="size-3.5 shrink-0" aria-hidden />
      {t("badge")}
    </span>
  );
}
