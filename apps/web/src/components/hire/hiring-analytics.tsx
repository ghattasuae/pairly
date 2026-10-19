// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
import { useFormatter, useTranslations } from "next-intl";
import { useId } from "react";

import { Link } from "@/i18n/navigation";
import { metricValue, recordHref } from "@/lib/hire/analytics";
import type { HireAnalytics, HireMetric, HireSurveyResults } from "@/lib/hire/types";

/** One value in its unit, or "No data yet" (a missing number is never shown as 0). */
export function MetricValue({ metric }: { metric: Pick<HireMetric, "value" | "unit"> }) {
  const t = useTranslations("HireAnalytics");
  const format = useFormatter();
  const parts = metricValue(metric);
  if (parts.kind === "none") return <span className="text-muted-foreground">{t("noData")}</span>;
  const { value, unit } = parts;
  let text: string;
  switch (unit) {
    case "rate":
      text = format.number(value, { style: "percent", maximumFractionDigits: 1 });
      break;
    case "hours":
      text = t("unit.hours", { value: format.number(value, { maximumFractionDigits: 1 }) });
      break;
    case "days":
      text = t("unit.days", { value: format.number(value, { maximumFractionDigits: 1 }) });
      break;
    case "minutes":
      text = t("unit.minutes", { value: format.number(value, { maximumFractionDigits: 0 }) });
      break;
    case "usd":
      text = format.number(value, { style: "currency", currency: "USD", maximumFractionDigits: 4 });
      break;
    default:
      text = format.number(value, { maximumFractionDigits: 2 });
  }
  return <span>{text}</span>;
}

/** The records a number was computed from, each linking to its page. */
export function Records({ metric }: { metric: HireMetric }) {
  const t = useTranslations("HireAnalytics");
  if (metric.records_total === 0) return null;
  return (
    <details className="text-xs" data-records={metric.key}>
      <summary className="cursor-pointer text-primary underline-offset-4 hover:underline">
        {t("records", { count: metric.records_total })}
      </summary>
      <ul className="mt-2 flex flex-wrap gap-1.5">
        {metric.records.map((r) => {
          const href = recordHref(r);
          return (
            <li key={`${r.kind}:${r.id}`}>
              {href ? (
                <Link href={href} className="rounded border px-1.5 py-0.5 font-mono hover:text-primary">
                  {r.label}
                </Link>
              ) : (
                <span className="rounded border px-1.5 py-0.5 font-mono">{r.label}</span>
              )}
            </li>
          );
        })}
      </ul>
      {metric.records_total > metric.records.length && (
        <p className="mt-1 text-muted-foreground">
          {t("recordsCapped", { shown: metric.records.length, total: metric.records_total })}
        </p>
      )}
    </details>
  );
}

function Tile({ title, metric, note }: { title: string; metric: HireMetric; note?: string | undefined }) {
  const t = useTranslations("HireAnalytics");
  return (
    <div className="space-y-1.5 card-hairline p-4" data-metric={metric.key}>
      <p className="text-sm text-muted-foreground">{title}</p>
      <p className="figure text-3xl leading-none">
        <MetricValue metric={metric} />
      </p>
      <p className="font-mono text-xs text-muted-foreground">{t("sample", { n: metric.n })}</p>
      {note && <p className="text-xs text-muted-foreground">{note}</p>}
      <Records metric={metric} />
    </div>
  );
}

function Section({ title, children, intro }: { title: string; intro?: string; children: React.ReactNode }) {
  const id = useId();
  return (
    <section aria-labelledby={id} className="space-y-3">
      <h2 id={id} className="font-display text-xl">
        {title}
      </h2>
      {intro && <p className="text-sm text-muted-foreground">{intro}</p>}
      {children}
    </section>
  );
}

function Rows({
  metrics,
  label,
  extra,
}: {
  metrics: HireMetric[];
  label: (m: HireMetric) => string;
  extra?: (m: HireMetric) => string | null;
}) {
  const t = useTranslations("HireAnalytics");
  if (metrics.length === 0) return <p className="text-sm text-muted-foreground">{t("noData")}</p>;
  return (
    <ul className="divide-y card-hairline">
      {metrics.map((m) => (
        <li key={m.key} className="space-y-1 p-3" data-metric={m.key}>
          <div className="flex flex-wrap items-baseline justify-between gap-2 text-sm">
            <span className="font-medium">{label(m)}</span>
            <span className="figure text-lg">
              <MetricValue metric={m} />
            </span>
          </div>
          <p className="text-xs text-muted-foreground">
            {t("sample", { n: m.n })}
            {extra?.(m) ? ` · ${extra(m)}` : ""}
          </p>
          <Records metric={m} />
        </li>
      ))}
    </ul>
  );
}

function detail(m: HireMetric, key: string): unknown {
  return (m.detail as Record<string, unknown>)[key];
}

export function SurveySummary({ survey }: { survey: HireSurveyResults }) {
  const t = useTranslations("HireAnalytics");
  const format = useFormatter();
  const overall = survey.overall.averages.overall;
  return (
    <div className="space-y-1.5 card-hairline p-4" data-metric="survey">
      <p className="text-sm text-muted-foreground">{t("survey.overall")}</p>
      <p className="figure text-3xl leading-none">
        {survey.overall.suppressed || overall === null || overall === undefined ? (
          <span className="text-muted-foreground">{t("survey.hidden", { min: survey.min_group })}</span>
        ) : (
          t("survey.outOf5", { value: format.number(overall, { maximumFractionDigits: 2 }) })
        )}
      </p>
      <p className="text-xs text-muted-foreground">
        {t("survey.counts", { responses: survey.responses, invited: survey.invited })}
      </p>
      <Link href="/hire/surveys" className="text-xs text-primary hover:underline" data-survey-link>
        {t("survey.open")}
      </Link>
    </div>
  );
}

/**
 * Hiring analytics for HR and talent leads and admins (brief 3.10, 5.1.4). Every number
 * comes from the API, computed from the organisation's own records, and opens the list
 * of records behind it (application references, never names). Estimates say so.
 */
export function HiringAnalytics({ data }: { data: HireAnalytics }) {
  const t = useTranslations("HireAnalytics");
  const stage = (m: HireMetric) => String(detail(m, "stage") ?? "");
  const [ghosting, approvals] = data.sla;
  const [agreement, disagreement] = data.agreement;
  const [injection, claims, signals] = data.flags;
  const cost = data.cost_per_screened_application;
  return (
    <div className="space-y-10" data-hiring-analytics>
      <p className="text-sm text-muted-foreground">{t("scope", { count: data.applications })}</p>

      <Section title={t("section.speed")}>
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
          <Tile
            title={t("metric.firstResponse")}
            metric={data.time_to_first_response}
            note={t("pending", { count: Number(detail(data.time_to_first_response, "pending") ?? 0) })}
          />
          <Tile title={t("metric.timeToHire")} metric={data.time_to_hire} />
          {ghosting && <Tile title={t("metric.slaGhosting", { days: data.settings.sla_days })} metric={ghosting} />}
          {approvals && <Tile title={t("metric.slaApprovals")} metric={approvals} />}
          <Tile title={t("metric.ghosting", { days: data.settings.ghost_days })} metric={data.ghosting_rate} />
        </div>
        <h3 className="pt-2 text-sm font-medium">{t("metric.timeInStage")}</h3>
        <Rows metrics={data.time_in_stage} label={stage} />
      </Section>

      <Section title={t("section.funnel")} intro={t("conversionMethod")}>
        <Rows
          metrics={data.stage_conversion}
          label={(m) => t("conversion", { from: stage(m), to: String(detail(m, "next_stage") ?? "") })}
          extra={(m) => t("reached", { reached: Number(detail(m, "reached") ?? 0), moved: Number(detail(m, "moved_on") ?? 0) })}
        />
        <h3 className="pt-2 text-sm font-medium">{t("metric.sources")}</h3>
        <Rows
          metrics={data.sources}
          label={(m) => String(detail(m, "source") ?? "")}
          extra={(m) =>
            t("sourceDetail", {
              applications: Number(detail(m, "applications") ?? 0),
              shortlisted: Number(detail(m, "shortlisted") ?? 0),
              hired: Number(detail(m, "hired") ?? 0),
            })
          }
        />
      </Section>

      <Section title={t("section.interviews")}>
        <Tile
          title={t("metric.timeSaved")}
          metric={data.scheduling_time_saved}
          note={t("timeSavedMethod", { minutes: data.settings.minutes_per_booking })}
        />
        <h3 className="pt-2 text-sm font-medium">{t("metric.interviewerLoad")}</h3>
        <Rows
          metrics={data.interviewer_load}
          label={(m) => String(detail(m, "name") || t("someone"))}
          extra={(m) => t("upcoming", { count: Number(detail(m, "upcoming") ?? 0) })}
        />
      </Section>

      <Section title={t("section.ai")}>
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
          {agreement && (
            <Tile
              title={t("metric.agreement")}
              metric={agreement}
              note={t("agreementMethod", { threshold: data.settings.agreement_threshold })}
            />
          )}
          {disagreement && <Tile title={t("metric.disagreement")} metric={disagreement} />}
          <Tile title={t("metric.overrides")} metric={data.override_rate} />
          {injection && <Tile title={t("metric.injection")} metric={injection} />}
          {claims && <Tile title={t("metric.claims")} metric={claims} note={t("infoOnly")} />}
          {signals && <Tile title={t("metric.signals")} metric={signals} note={t("infoOnly")} />}
          <Tile
            title={t("metric.cost")}
            metric={cost}
            note={detail(cost, "recorded_mode") ? t("recordedMode") : undefined}
          />
        </div>
      </Section>

      {data.survey && (
        <Section title={t("section.survey")}>
          <SurveySummary survey={data.survey} />
        </Section>
      )}
    </div>
  );
}
