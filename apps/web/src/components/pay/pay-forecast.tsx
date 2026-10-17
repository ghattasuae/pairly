// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
import { useFormatter, useTranslations } from "next-intl";

import { Link } from "@/i18n/navigation";
import type { PayForecast, PayForecastBucket, PayForecastCurrency } from "@/lib/pay/types";

function Money({ currency, amount }: { currency: string; amount: string }) {
  return (
    <span className="font-mono tabular-nums" dir="ltr">
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
      <summary className="cursor-pointer text-primary hover:underline">
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
  return (
    <section className="space-y-2" data-forecast-currency={cur}>
      <h2 className="font-display text-xl">
        {t("currency", {
          currency: cur,
          committed: `${cur} ${data.committed_total}`,
          review: `${cur} ${data.in_review_total}`,
        })}
      </h2>
      <div className="overflow-x-auto rounded-lg border bg-surface">
        <table className="w-full text-sm">
          <thead>
            <tr className="text-xs text-muted-foreground">
              <th className="px-3 py-2 text-start font-medium">{t("col.week")}</th>
              <th className="px-3 py-2 text-start font-medium">{t("col.committed")}</th>
              <th className="px-3 py-2 text-start font-medium">{t("col.inReview")}</th>
            </tr>
          </thead>
          <tbody className="divide-y">
            {data.weeks.map((week) => (
              <tr key={week.index} data-week={week.index}>
                <td className="px-3 py-2 align-top">
                  {week.index === 0 ? t("thisWeek") : t("week", { start: day(week.start), end: day(week.end) })}
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
                <td className="px-3 py-2 align-top text-muted-foreground">{row.label}</td>
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
                <Link href={`/pay/invoices/${invoice.id}`} className="rounded border px-1.5 py-0.5 font-mono" dir="ltr">
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
