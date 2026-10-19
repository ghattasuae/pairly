// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
import { useTranslations } from "next-intl";

import { Badge } from "@/components/ui/badge";
import type { HireKit } from "@/lib/hire/types";

/**
 * The interview kit (brief 3.7.1, 5.3): readable on a phone during or right after the
 * call. One card per rubric criterion: the questions, probes for weak or unclear
 * evidence from screening, what a strong answer shows, and the 1 to 4 anchors. CV quotes
 * appear only once the candidate's identity is shown (the API leaves them out while blind).
 */
export function InterviewKit({ kit }: { kit: HireKit }) {
  const t = useTranslations("InterviewKit");
  return (
    <section aria-label={t("title")} className="space-y-3" data-interview-kit data-source={kit.source}>
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h2 className="font-display text-lg">{t("title")}</h2>
        <span className="text-xs text-muted-foreground">
          {kit.source === "template" ? t("templateNote") : t("aiNote")}
        </span>
      </div>
      <ol className="space-y-3">
        {kit.items.map((item, n) => (
          <li key={item.criterion_id} className="space-y-2 card-hairline p-4 text-[0.95rem] leading-relaxed" data-kit-item={item.criterion_id}>
            <p className="flex flex-wrap items-center gap-2">
              <span className="font-medium">
                {n + 1}. {item.criterion_name}
              </span>
              <Badge variant={item.screening_result === "met" ? "default" : "outline"}>
                {t(`result.${item.screening_result}` as "result.met")}
              </Badge>
            </p>
            <ul className="list-disc space-y-1 ps-5">
              {item.questions.map((q) => (
                <li key={q}>{q}</li>
              ))}
            </ul>
            {item.probes.length > 0 && (
              <div>
                <p className="text-xs font-medium text-muted-foreground uppercase">{t("probes")}</p>
                <ul className="list-disc space-y-1 ps-5">
                  {item.probes.map((p) => (
                    <li key={p}>{p}</li>
                  ))}
                </ul>
              </div>
            )}
            {item.listen_for && <p className="text-sm text-muted-foreground">{item.listen_for}</p>}
            {item.quotes.length > 0 && (
              <blockquote className="border-s-2 ps-3 text-sm text-muted-foreground">
                {item.quotes.map((q) => (
                  <p key={q}>&ldquo;{q}&rdquo;</p>
                ))}
              </blockquote>
            )}
          </li>
        ))}
      </ol>
    </section>
  );
}
