// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
import { useFormatter, useTranslations } from "next-intl";
import { useId } from "react";

import type { HireSurveyGroup, HireSurveyNamed, HireSurveyResults } from "@/lib/hire/types";

const QUESTIONS = ["overall", "communication", "respect"] as const;
const OUTCOMES = ["hired", "rejected", "withdrawn"] as const;

function Group({
  title,
  group,
  min,
  level = "h3",
}: {
  title: string;
  group: HireSurveyGroup;
  min: number;
  level?: "h2" | "h3";
}) {
  const t = useTranslations("HireSurveyResults");
  const format = useFormatter();
  const id = useId();
  const Heading = level;
  return (
    <section aria-labelledby={id} className="space-y-2 card-hairline p-4" data-survey-group>
      <Heading id={id} className={level === "h2" ? "font-display text-xl" : "font-medium"}>
        {title}
      </Heading>
      {group.suppressed ? (
        <p className="text-sm text-muted-foreground">{t("hidden", { count: group.responses, min })}</p>
      ) : (
        <dl className="grid gap-x-4 gap-y-1 text-sm sm:grid-cols-2">
          <dt className="text-muted-foreground">{t("responses")}</dt>
          <dd>{group.responses}</dd>
          {QUESTIONS.map((q) => {
            const value = group.averages[q];
            return (
              <div key={q} className="contents">
                <dt className="text-muted-foreground">{t(`question.${q}`)}</dt>
                <dd>
                  {value === null || value === undefined
                    ? t("noAnswer")
                    : t("outOf5", { value: format.number(value, { maximumFractionDigits: 2 }) })}
                </dd>
              </div>
            );
          })}
          <dt className="text-muted-foreground">{t("recommend")}</dt>
          <dd>
            {t("recommendCounts", {
              yes: group.recommend.yes ?? 0,
              no: group.recommend.no ?? 0,
              unsure: group.recommend.unsure ?? 0,
            })}
          </dd>
        </dl>
      )}
    </section>
  );
}

/**
 * Candidate experience survey results for HR, talent leads and admins (brief 3.8.5,
 * 5.1.4), from the API's totals (`survey.service.aggregate_results`): small groups are
 * hidden, single answers never shown, except the ones whose authors attached their name.
 */
export function SurveyResults({ results, named }: { results: HireSurveyResults; named: HireSurveyNamed[] | null }) {
  const t = useTranslations("HireSurveyResults");
  const format = useFormatter();
  return (
    <div className="space-y-8" data-survey-results>
      <div className="grid gap-3 sm:grid-cols-3">
        <div className="card-hairline p-4">
          <p className="text-sm text-muted-foreground">{t("invited")}</p>
          <p className="figure text-3xl leading-none" data-survey-stat="invited">
            {results.invited}
          </p>
        </div>
        <div className="card-hairline p-4">
          <p className="text-sm text-muted-foreground">{t("responses")}</p>
          <p className="figure text-3xl leading-none" data-survey-stat="responses">
            {results.responses}
          </p>
        </div>
        <div className="card-hairline p-4">
          <p className="text-sm text-muted-foreground">{t("rate")}</p>
          <p className="figure text-3xl leading-none" data-survey-stat="rate">
            {results.response_rate === null
              ? t("noAnswer")
              : format.number(results.response_rate, { style: "percent", maximumFractionDigits: 0 })}
          </p>
        </div>
      </div>
      <Group title={t("overall")} group={results.overall} min={results.min_group} level="h2" />
      <div className="space-y-3">
        <h2 className="font-display text-xl">{t("byOutcome")}</h2>
        <div className="grid gap-3 md:grid-cols-3">
          {OUTCOMES.map((o) => {
            const group = results.by_outcome[o];
            return group ? <Group key={o} title={t(`outcome.${o}`)} group={group} min={results.min_group} /> : null;
          })}
        </div>
      </div>
      {named !== null && (
        <section className="space-y-3" data-survey-named>
          <h2 className="font-display text-xl">{t("named")}</h2>
          <p className="text-sm text-muted-foreground">{t("namedIntro")}</p>
          {named.length === 0 ? (
            <p className="text-sm text-muted-foreground">{t("noNamed")}</p>
          ) : (
            <ul className="divide-y card-hairline">
              {named.map((n) => (
                <li key={n.response_id} className="space-y-1 p-3 text-sm">
                  <p className="text-muted-foreground">
                    {t(`outcome.${n.outcome}` as "outcome.hired")} ·{" "}
                    {t("answeredOn", { date: format.dateTime(new Date(`${n.answered_on}T00:00:00Z`), { dateStyle: "medium", timeZone: "UTC" }) })}
                  </p>
                  <p>
                    {QUESTIONS.filter((q) => typeof n.answers[q] === "number")
                      .map((q) => `${t(`question.${q}`)}: ${n.answers[q]}`)
                      .join(" · ")}
                  </p>
                  {n.comment && (
                    <p>
                      <span className="text-muted-foreground">{t("comment")}: </span>
                      {n.comment}
                    </p>
                  )}
                </li>
              ))}
            </ul>
          )}
        </section>
      )}
    </div>
  );
}
