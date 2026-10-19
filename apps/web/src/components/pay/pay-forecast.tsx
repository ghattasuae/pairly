// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
import { useFormatter, useTranslations } from "next-intl";

import { Link } from "@/i18n/navigation";
import type { PayForecast, PayForecastBucket, PayForecastCurrency } from "@/lib/pay/types";

function Money({ currency, amount }: { currency: string; amount: string }) {
  return (
    <span className="font-mono tabular-nums" dir="ltr" data-amount>
      {currency} {amount}
    </span>
  );
}

/**
 * One number of the forecast and the invoices it adds up, each linking to its page.
 * An empty bucket reads as a dash, never as a link to nothing.
 */
function Amount({ bucket, currency, testId }: { bucket: PayForecastBucket; currency: string; testId: string }) {
  const t = useTranslations("Pay.forecast");
  const format = useFormatter();
  if (bucket.count === 0) {
    return (
      <span className="text-muted-foreground" data-bucket={testId} data-empty>
        -
      </span>
    );
  }
  return (
    <details className="text-sm" data-bucket={testId}>
      <summary className="cursor-pointer text-primary underline decoration-primary/40 underline-offset-4 hover:decoration-2">
        <Money currency={currency} amount={bucket.amount} />
      </summary>
      <p className="mt-1 text-xs text-muted-foreground">
        {t("invoices", { count: bucket.count })}
        {Number(bucket.credits) > 0 && <> · {t("credits", { credits: `${currency} ${bucket.credits}` })}</>}
      </p>
      <ul className="mt-1 space-y-0.5 text-xs">
        {bucket.invoices.map((invoice) => (
          <li key={invoice.id} className="flex flex-wrap gap-x-2">
            <Link href={`/pay/invoices/${invoice.id}`} className="font-mono underline-offset-4 hover:underline" dir="ltr">
              {invoice.reference}
            </Link>
            <bdi className="text-muted-foreground">{invoice.vendor_name ?? ""}</bdi>
            {invoice.amount && <Money currency={currency} amount={invoice.amount} />}
            {invoice.expected_date && (
              <span className="text-muted-foreground">
                {t("expected", {
                  when: format.dateTime(new Date(`${invoice.expected_date}T00:00:00Z`), {
                    dateStyle: "medium",
                    timeZone: "UTC",
                  }),
                })}
              </span>
            )}
          </li>
        ))}
      </ul>
    </details>
  );
}

function CurrencyTable({ data }: { data: PayForecastCurrency }) {
  const t = useTranslations("Pay.forecast");
  const format = useFormatter();
  const day = (iso: string) =>
    format.dateTime(new Date(`${iso}T00:00:00Z`), { day: "numeric", month: "short", timeZone: "UTC" });
  const cur = data.currency;
  const extra = [
    { key: "later", label: t("later"), committed: data.later_committed, inReview: data.later_in_review },
    { key: "undated", label: t("undated"), committed: data.undated_committed, inReview: data.undated_in_review },
  ].filter((row) => row.committed.count > 0 || row.inReview.count > 0);
  // Bars share one scale per currency: the largest week (committed plus in review).
  const max = Math.max(
    1,
    ...data.weeks.map((w) => Number(w.committed.amount) + Number(w.in_review.amount)),
    ...extra.map((r) => Number(r.committed.amount) + Number(r.inReview.amount)),
  );
  return (
    <section className="space-y-3" data-forecast-currency={cur}>
      <h2 className="font-display text-xl">
        {t("currency", {
          currency: cur,
          committed: `${cur} ${data.committed_total}`,
          review: `${cur} ${data.in_review_total}`,
        })}
      </h2>
      <ForecastLegend />
      <div className="overflow-x-auto rounded-lg border border-border bg-surface">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-border bg-surface-sunken text-xs text-muted-foreground">
              <th className="px-3 py-2 text-start font-medium">{t("col.week")}</th>
              <th className="px-3 py-2 text-start font-medium">{t("col.committed")}</th>
              <th className="px-3 py-2 text-start font-medium">{t("col.inReview")}</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-border">
            {data.weeks.map((week) => (
              <tr key={week.index} data-week={week.index}>
                <td className="px-3 py-2 align-top">
                  <span className="block font-mono text-xs">
                    {week.index === 0 ? t("thisWeek") : t("week", { start: day(week.start), end: day(week.end) })}
                  </span>
                  <WeekBar committed={Number(week.committed.amount)} inReview={Number(week.in_review.amount)} max={max} />
                </td>
                <td className="px-3 py-2 align-top">
                  <Amount bucket={week.committed} currency={cur} testId={`${cur}:${week.index}:committed`} />
                </td>
                <td className="px-3 py-2 align-top">
                  <Amount bucket={week.in_review} currency={cur} testId={`${cur}:${week.index}:in_review`} />
                </td>
              </tr>
            ))}
            {extra.map((row) => (
              <tr key={row.key} data-week={row.key}>
                <td className="px-3 py-2 align-top text-muted-foreground">
                  {row.label}
                  <WeekBar committed={Number(row.committed.amount)} inReview={Number(row.inReview.amount)} max={max} />
                </td>
                <td className="px-3 py-2 align-top">
                  <Amount bucket={row.committed} currency={cur} testId={`${cur}:${row.key}:committed`} />
                </td>
                <td className="px-3 py-2 align-top">
                  <Amount bucket={row.inReview} currency={cur} testId={`${cur}:${row.key}:in_review`} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

/**
 * The two series, named once above the table. Same slate hue, told apart by fill: solid
 * for committed, an outlined light tint for money still in review, so the pair reads in
 * both themes and in greyscale.
 */
function ForecastLegend() {
  const t = useTranslations("Pay.forecast");
  return (
    <p className="flex flex-wrap gap-x-5 gap-y-1 text-xs text-muted-foreground" aria-hidden>
      <span className="inline-flex items-center gap-1.5">
        <span className="inline-block h-2.5 w-4 rounded-sm bg-primary" />
        {t("col.committed")}
      </span>
      <span className="inline-flex items-center gap-1.5">
        <span className="inline-block h-2.5 w-4 rounded-sm border border-primary bg-info-soft" />
        {t("col.inReview")}
      </span>
    </p>
  );
}

/** A thin stacked bar for one week. Decorative: the numbers sit in the same row. */
function WeekBar({ committed, inReview, max }: { committed: number; inReview: number; max: number }) {
  const c = Number.isFinite(committed) ? Math.max(0, committed) : 0;
  const r = Number.isFinite(inReview) ? Math.max(0, inReview) : 0;
  if (c + r === 0) return null;
  return (
    <span aria-hidden className="mt-1.5 flex h-2 w-full max-w-48 gap-0.5" data-week-bar>
      {c > 0 && <span className="rounded-sm bg-primary" style={{ inlineSize: `${(c / max) * 100}%` }} />}
      {r > 0 && <span className="rounded-sm border border-primary bg-info-soft" style={{ inlineSize: `${(r / max) * 100}%` }} />}
    </span>
  );
}

/**
 * The cash forecast (brief 4.6): upcoming payables by week for each currency, net of
 * applied credits, with invoices still in review apart; every number opens its invoices.
 */
export function PayForecastView({ data }: { data: PayForecast }) {
  const t = useTranslations("Pay.forecast");
  return (
    <div className="space-y-8" data-pay-forecast>
      {data.currencies.length === 0 ? (
        <p className="text-sm text-muted-foreground" data-empty>
          {t("empty")}
        </p>
      ) : (
        data.currencies.map((currency) => <CurrencyTable key={currency.currency} data={currency} />)
      )}
      {data.unpriced.length > 0 && (
        <details className="text-sm" data-unpriced>
          <summary className="cursor-pointer">{t("unpriced", { count: data.unpriced.length })}</summary>
          <ul className="mt-1 flex flex-wrap gap-1.5">
            {data.unpriced.map((invoice) => (
              <li key={invoice.id}>
                <Link href={`/pay/invoices/${invoice.id}`} className="rounded-sm border border-border bg-surface px-1.5 py-0.5 font-mono hover:border-primary" dir="ltr">
                  {invoice.reference}
                </Link>
              </li>
            ))}
          </ul>
        </details>
      )}
      <p className="text-xs text-muted-foreground">{t("method")}</p>
    </div>
  );
}
