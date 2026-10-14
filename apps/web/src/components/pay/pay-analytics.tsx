// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
import { useFormatter, useTranslations } from "next-intl";
import { useId } from "react";
import type * as React from "react";

import { Link } from "@/i18n/navigation";
import { keyTail, metricValue, recordHref } from "@/lib/pay/analytics";
import type { PayAnalytics, PayMetric } from "@/lib/pay/types";

function detail(m: PayMetric, key: string): unknown {
  return (m.detail as Record<string, unknown>)[key];
}

/** One value in its unit, or "No data" (a missing number is never shown as 0). */
export function PayMetricValue({ metric }: { metric: PayMetric }) {
  const t = useTranslations("Pay.analytics");
  const format = useFormatter();
  const parts = metricValue(metric);
  if (parts.kind === "none") {
    return (
      <span className="text-muted-foreground" data-no-data>
        {t("noData")}
      </span>
    );
  }
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
    case "usd":
      text = format.number(value, { style: "currency", currency: "USD", maximumFractionDigits: 4 });
      break;
    case "amount": {
      const currency = String(detail(metric, "currency") ?? "");
      text = /^[A-Z]{3}$/.test(currency)
        ? format.number(value, { style: "currency", currency, maximumFractionDigits: 2 })
        : format.number(value, { maximumFractionDigits: 2 });
      break;
    }
    default:
      text = format.number(value, { maximumFractionDigits: 2 });
  }
  return <span data-value>{text}</span>;
}

/** The records a number was computed from, each linking to its page. */
function Records({ metric }: { metric: PayMetric }) {
  const t = useTranslations("Pay.analytics");
  if (metric.records_total === 0) return null;
  return (
    <details className="text-xs" data-records={metric.key}>
      <summary className="cursor-pointer text-primary hover:underline">
        {t("records", { count: metric.records_total })}
      </summary>
      <ul className="mt-2 flex flex-wrap gap-1.5">
        {metric.records.map((r) => {
          const href = recordHref(r);
          return (
            <li key={`${r.kind}:${r.id}`}>
              {href ? (
                <Link href={href} className="rounded border px-1.5 py-0.5 font-mono hover:text-primary" dir="ltr">
                  {r.label}
                </Link>
              ) : (
                <span className="rounded border px-1.5 py-0.5 font-mono" dir="ltr">
                  {r.label}
                </span>
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

function Tile({ title, metric, note }: { title: string; metric: PayMetric; note?: string | undefined }) {
  const t = useTranslations("Pay.analytics");
  return (
    <div className="space-y-1.5 rounded-lg border bg-surface p-4" data-metric={metric.key}>
      <p className="text-sm text-muted-foreground">{title}</p>
      <p className="font-display text-2xl">
        <PayMetricValue metric={metric} />
      </p>
      <p className="text-xs text-muted-foreground">{t("sample", { n: metric.n })}</p>
      {note && <p className="text-xs text-muted-foreground">{note}</p>}
      <Records metric={metric} />
    </div>
  );
}

function Section({ title, intro, children }: { title: string; intro?: string; children: React.ReactNode }) {
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

function Rows({ metrics, label }: { metrics: PayMetric[]; label: (m: PayMetric) => string }) {
  const t = useTranslations("Pay.analytics");
  if (metrics.length === 0) {
    return (
      <p className="text-sm text-muted-foreground" data-no-data>
        {t("noData")}
      </p>
    );
  }
  return (
    <ul className="divide-y rounded-lg border bg-surface">
      {metrics.map((m) => (
        <li key={m.key} className="space-y-1 p-3" data-metric={m.key}>
          <div className="flex flex-wrap items-baseline justify-between gap-2 text-sm">
            <span className="font-medium">{label(m)}</span>
            <span className="font-display text-lg">
              <PayMetricValue metric={m} />
            </span>
          </div>
          <p className="text-xs text-muted-foreground">{t("sample", { n: m.n })}</p>
          <Records metric={m} />
        </li>
      ))}
    </ul>
  );
}

/**
 * The Pay analytics (brief 4.9): every number from the organisation's own tables, each
 * with the records behind it, and "No data" when there is nothing to count.
 */
export function PayAnalyticsView({ data }: { data: PayAnalytics }) {
  const t = useTranslations("Pay.analytics");
  const cost = data.cost_per_invoice;
  const [count, ...values] = data.duplicates_prevented;
  const [allExceptions, ...categories] = data.exception_rate;
  const [allFlags, ...outcomes] = data.fraud_flags;
  const categoryLabel = (m: PayMetric) => {
    const key = keyTail(m.key);
    if (key.startsWith("check.")) {
      const check = key.slice(6);
      return t.has(`checks.${check}` as "checks.bank")
        ? t("categoryCheck", { check: t(`checks.${check}` as "checks.bank") })
        : t("categoryCheck", { check });
    }
    return t.has(`category.${key}` as "category.reading") ? t(`category.${key}` as "category.reading") : key;
  };
  return (
    <div className="space-y-8" data-pay-analytics>
      <p className="text-sm text-muted-foreground">{t("invoices", { count: data.invoices })}</p>

      <Section title={t("flowTitle")}>
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
          <Tile title={t("touchless")} metric={data.touchless_rate} note={t("touchlessNote")} />
          {data.cycle_time.map((m) => (
            <Tile key={m.key} title={t(`cycle.${keyTail(m.key)}` as "cycle.receipt_to_approval")} metric={m} note={t("medianNote")} />
          ))}
          <Tile
            title={t("cost")}
            metric={cost}
            note={detail(cost, "recorded_mode") ? t("recordedMode") : t("costNote")}
          />
        </div>
      </Section>

      <Section title={t("exceptionsTitle")} intro={t("exceptionsIntro")}>
        {allExceptions && <Tile title={t("exceptionRate")} metric={allExceptions} />}
        <Rows metrics={categories} label={categoryLabel} />
      </Section>

      <Section title={t("controlsTitle")}>
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
          {count && <Tile title={t("duplicates")} metric={count} note={t("duplicatesNote")} />}
          <Tile title={t("bankBlocked")} metric={data.bank_changes_blocked} note={t("bankBlockedNote")} />
          {allFlags && <Tile title={t("fraudFlags")} metric={allFlags} note={t("fraudNote")} />}
        </div>
        <h3 className="text-sm font-medium">{t("duplicatesValue")}</h3>
        <Rows metrics={values} label={(m) => String(detail(m, "currency") ?? keyTail(m.key))} />
        <h3 className="text-sm font-medium">{t("fraudOutcomes")}</h3>
        <Rows metrics={outcomes} label={(m) => t(`outcome.${keyTail(m.key)}` as "outcome.open")} />
      </Section>

      <Section title={t("openPayablesTitle")} intro={t("openPayablesIntro")}>
        <Rows
          metrics={data.open_payables}
          label={(m) =>
            `${String(detail(m, "currency") ?? keyTail(m.key))} · ${t("openPayablesCredits", {
              gross: String(detail(m, "gross") ?? ""),
              credits: String(detail(m, "credits_applied") ?? ""),
            })}`
          }
        />
      </Section>

      <Section title={t("paymentTitle")}>
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
          <Tile title={t("slaBreaches")} metric={data.approval_sla_breaches} note={t("slaNote")} />
          <Tile title={t("onTime")} metric={data.on_time_payment_rate} note={t("onTimeNote")} />
          <Tile title={t("dpo")} metric={data.days_payable_outstanding} note={t("dpoNote")} />
        </div>
      </Section>
    </div>
  );
}
