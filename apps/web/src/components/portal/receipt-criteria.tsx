// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
import { useTranslations } from "next-intl";

export interface ReceiptCriterion {
  criterion_id: string;
  name: string;
  score: number;
  scale_max: number;
  anchor: string | null;
  your_evidence: string[];
}

export interface ReceiptVersions {
  model: string | null;
  prompts: { task?: string | null; version?: string | null }[];
}

/**
 * A screening on the candidate's receipt: per criterion, the outcome on the scale, what
 * that score means in the job's rubric, and the candidate's own words it rests on. Then
 * the model and prompt version. Nothing internal reaches this view (the API decides).
 */
export function ReceiptCriteria({ criteria, versions }: { criteria: ReceiptCriterion[]; versions?: ReceiptVersions | null }) {
  const t = useTranslations("Portal.privacy");
  return (
    <section aria-labelledby="receipt-criteria" className="space-y-3" data-receipt-criteria>
      <h2 id="receipt-criteria" className="font-display text-xl">
        {t("criteriaTitle")}
      </h2>
      <ul className="space-y-3">
        {criteria.map((c) => (
          <li key={c.criterion_id} className="space-y-2 rounded-xl border bg-surface p-4" data-criterion={c.criterion_id}>
            <p className="flex flex-wrap items-baseline justify-between gap-2">
              <span className="font-medium">{c.name}</span>
              <span className="text-sm text-muted-foreground">{t("criterionScore", { score: c.score, max: c.scale_max })}</span>
            </p>
            {c.anchor && <p className="text-sm">{t("criterionMeans", { anchor: c.anchor })}</p>}
            {c.your_evidence.length > 0 ? (
              <div className="space-y-1">
                <p className="text-xs text-muted-foreground">{t("yourWords")}</p>
                <ul className="space-y-1">
                  {c.your_evidence.map((q, i) => (
                    <li key={i}>
                      <blockquote className="border-s-2 ps-3 text-sm italic">{q}</blockquote>
                    </li>
                  ))}
                </ul>
              </div>
            ) : (
              <p className="text-xs text-muted-foreground">{t("noWords")}</p>
            )}
          </li>
        ))}
      </ul>
      {versions && (versions.model || versions.prompts.length > 0) && (
        <p className="text-xs text-muted-foreground" data-receipt-versions>
          {t("versions", {
            model: versions.model ?? "-",
            prompts: versions.prompts.map((p) => `${p.task ?? ""} ${p.version ?? ""}`.trim()).join(", ") || "-",
          })}
        </p>
      )}
    </section>
  );
}
