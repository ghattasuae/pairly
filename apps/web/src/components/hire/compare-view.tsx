// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
"use client";

import { useTranslations } from "next-intl";

import { Dialog, DialogContent, DialogDescription, DialogTitle, dialogTitleClass } from "@/components/ui/dialog";
import type { CandidateDetail } from "@/lib/hire/screening";

/**
 * Two or three candidates side by side on the same rubric (brief 5.3): one row per
 * criterion, each candidate's score and first supporting quote, and the overall score.
 * Labels are the reviewer's view (blind labels while blind).
 */
export function CompareView({
  open,
  onOpenChange,
  candidates,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  candidates: { id: string; label: string; detail: CandidateDetail | undefined }[];
}) {
  const t = useTranslations("Screen");
  const rows = new Map<string, string>();
  for (const c of candidates) {
    for (const criterion of c.detail?.screening?.criteria ?? []) {
      if (!rows.has(criterion.criterion_id)) rows.set(criterion.criterion_id, criterion.name);
    }
  }
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="top-[6vh] max-h-[88vh] max-w-5xl overflow-y-auto" data-compare>
        <div className="space-y-4 p-6">
          <DialogTitle className={dialogTitleClass}>{t("compare.title")}</DialogTitle>
          <DialogDescription className="sr-only">{t("compare.title")}</DialogDescription>
          <div className="table-frame">
            <table className="data-table">
              <caption className="sr-only">{t("compare.title")}</caption>
              <thead>
                <tr>
                  <th scope="col">
                    {t("compare.criterion")}
                  </th>
                  {candidates.map((c) => (
                    <th key={c.id} scope="col" className="text-foreground" data-compare-candidate={c.id}>
                      {c.label}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                <tr>
                  <th scope="row">
                    {t("score.overall")}
                  </th>
                  {candidates.map((c) => {
                    const overall = c.detail?.screening?.overall_score;
                    return (
                      <td key={c.id} className="figure text-2xl leading-none">
                        {overall === null || overall === undefined
                          ? t("why.notScored")
                          : t("score.overallValue", { score: Math.round(overall) })}
                      </td>
                    );
                  })}
                </tr>
                {[...rows.entries()].map(([id, name]) => (
                  <tr key={id} data-compare-row={id}>
                    <th scope="row">
                      {name}
                    </th>
                    {candidates.map((c) => {
                      const criterion = c.detail?.screening?.criteria.find((x) => x.criterion_id === id);
                      const max = c.detail?.screening?.scale.max ?? 4;
                      return (
                        <td key={c.id}>
                          {criterion ? (
                            <div className="space-y-1">
                              <p className="font-mono font-medium">
                                {t("score.of", { score: criterion.score, max })}
                              </p>
                              <p className="line-clamp-3 border-s-2 border-rule ps-2 text-xs leading-relaxed text-muted-foreground">
                                {criterion.evidence[0]?.quote ?? t("score.noEvidence")}
                              </p>
                            </div>
                          ) : (
                            <span className="text-muted-foreground">-</span>
                          )}
                        </td>
                      );
                    })}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}
