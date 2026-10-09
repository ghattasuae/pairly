// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
"use client";

import { useTranslations } from "next-intl";

import { Dialog, DialogContent, DialogDescription, DialogTitle } from "@/components/ui/dialog";
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
          <DialogTitle className="font-display text-2xl">{t("compare.title")}</DialogTitle>
          <DialogDescription className="sr-only">{t("compare.title")}</DialogDescription>
          <div className="overflow-x-auto">
            <table className="w-full border-collapse text-sm">
              <caption className="sr-only">{t("compare.title")}</caption>
              <thead>
                <tr>
                  <th scope="col" className="border-b p-2 text-start font-medium">
                    {t("compare.criterion")}
                  </th>
                  {candidates.map((c) => (
                    <th key={c.id} scope="col" className="border-b p-2 text-start font-medium" data-compare-candidate={c.id}>
                      {c.label}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                <tr>
                  <th scope="row" className="border-b p-2 text-start font-medium">
                    {t("score.overall")}
                  </th>
                  {candidates.map((c) => {
                    const overall = c.detail?.screening?.overall_score;
                    return (
                      <td key={c.id} className="border-b p-2 font-display text-xl tabular-nums">
                        {overall === null || overall === undefined
                          ? t("why.notScored")
                          : t("score.overallValue", { score: Math.round(overall) })}
                      </td>
                    );
                  })}
                </tr>
                {[...rows.entries()].map(([id, name]) => (
                  <tr key={id} data-compare-row={id}>
                    <th scope="row" className="border-b p-2 text-start align-top font-medium">
                      {name}
                    </th>
                    {candidates.map((c) => {
                      const criterion = c.detail?.screening?.criteria.find((x) => x.criterion_id === id);
                      const max = c.detail?.screening?.scale.max ?? 4;
                      return (
                        <td key={c.id} className="border-b p-2 align-top">
                          {criterion ? (
                            <div className="space-y-1">
                              <p className="font-medium tabular-nums">
                                {t("score.of", { score: criterion.score, max })}
                              </p>
                              <p className="line-clamp-3 text-xs text-muted-foreground">
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
