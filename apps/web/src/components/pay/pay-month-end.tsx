// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
"use client";

import { Download, Loader2 } from "lucide-react";
import { useFormatter, useTranslations } from "next-intl";
import { useState } from "react";
import type * as React from "react";

import { Button } from "@/components/ui/button";
import { Link } from "@/i18n/navigation";
import { saveDownload, type DownloadResult } from "@/lib/pay/download";
import type { PayAgingBucket, PayJournalExport, PayMonthEnd } from "@/lib/pay/types";

import { ErrorText, Panel } from "./common";

const CATEGORIES = new Set([
  "no_po",
  "price_variance",
  "quantity_variance",
  "missing_receipt",
  "duplicate_suspect",
  "bank_mismatch",
  "policy_breach",
  "low_confidence",
  "other",
]);

function Money({ currency, amount }: { currency: string; amount: string }) {
  return (
    <span className="font-mono tabular-nums" dir="ltr">
      {currency} {amount}
    </span>
  );
}

/** One aging figure and the invoices it adds up; an empty bucket reads as a dash. */
function BucketCell({ bucket, currency }: { bucket: PayAgingBucket; currency: string }) {
  const t = useTranslations("Pay.monthEnd");
  if (bucket.count === 0) {
    return (
      <span className="text-muted-foreground" data-aging={bucket.bucket} data-empty>
        -
      </span>
    );
  }
  return (
    <details className="text-sm" data-aging={bucket.bucket}>
      <summary className="cursor-pointer text-primary hover:underline">
        <Money currency={currency} amount={bucket.amount} />
      </summary>
      <p className="mt-1 text-xs text-muted-foreground">{t("invoices", { count: bucket.count })}</p>
      <ul className="mt-1 space-y-0.5 text-xs">
        {bucket.invoices.map((inv) => (
          <li key={inv.id} className="flex flex-wrap gap-x-2">
            <Link href={`/pay/invoices/${inv.id}`} className="font-mono underline-offset-4 hover:underline" dir="ltr">
              {inv.reference}
            </Link>
            <bdi className="text-muted-foreground">{inv.vendor_name ?? ""}</bdi>
            {inv.amount && <Money currency={currency} amount={inv.amount} />}
          </li>
        ))}
      </ul>
    </details>
  );
}

export type MonthEndDownload = (kind: "report" | "journal", period: string) => Promise<DownloadResult>;

function defaultDownload(kind: "report" | "journal", period: string): Promise<DownloadResult> {
  const url = kind === "report" ? `/api/pay/month-end/export?period=${period}` : `/api/pay/journal/export?period=${period}`;
  return saveDownload(url, kind === "report" ? `month-end-${period}.csv` : `journal-${period}.csv`);
}

function DownloadButton({
  kind,
  period,
  download,
  children,
}: {
  kind: "report" | "journal";
  period: string;
  download: MonthEndDownload;
  children: React.ReactNode;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  async function run() {
    setBusy(true);
    setError(null);
    const result = await download(kind, period);
    setBusy(false);
    if (!result.ok) setError(result.code);
  }
  return (
    <div className="space-y-1">
      <Button type="button" size="sm" variant="outline" disabled={busy} onClick={run} data-download={kind}>
        {busy ? <Loader2 aria-hidden className="animate-spin" /> : <Download aria-hidden />}
        {children}
      </Button>
      <ErrorText code={error} />
    </div>
  );
}

/**
 * The month-end report: open payables at the period end by aging bucket, accrual
 * suggestions for goods received not invoiced, and the exceptions still open among
 * invoices received by then. Every figure opens the records it adds up.
 */
export function PayMonthEndView({
  data,
  exports = [],
  download = defaultDownload,
}: {
  data: PayMonthEnd;
  exports?: PayJournalExport[];
  download?: MonthEndDownload;
}) {
  const t = useTranslations("Pay.monthEnd");
  const format = useFormatter();
  const asOf = format.dateTime(new Date(data.as_of), { dateStyle: "medium", timeZone: "UTC" });
  const buckets = ["current", "1_30", "31_60", "61_90", "90_plus", "undated"] as const;
  return (
    <div className="space-y-6" data-month-end={data.period}>
      <div className="flex flex-wrap items-start gap-3">
        <p className="me-auto text-sm text-muted-foreground">{t("asOf", { when: asOf })}</p>
        <DownloadButton kind="report" period={data.period} download={download}>
          {t("downloadReport")}
        </DownloadButton>
        {data.can_export_journal && (
          <DownloadButton kind="journal" period={data.period} download={download}>
            {t("downloadJournal")}
          </DownloadButton>
        )}
      </div>

      {exports.length > 0 && (
        <div className="rounded-lg border p-3 text-sm" data-journal-exports>
          <p className="font-medium">{t("exportsTitle")}</p>
          <ul className="mt-1 space-y-0.5 text-xs text-muted-foreground">
            {exports.map((e) => (
              <li key={e.id} className="flex flex-wrap gap-x-3">
                <span>{format.dateTime(new Date(e.created_at), { dateStyle: "medium", timeStyle: "short", timeZone: "UTC" })}</span>
                <span>{t("exportCounts", { entries: e.entry_count, lines: e.line_count })}</span>
                <code className="font-mono" dir="ltr" title={e.sha256}>
                  SHA-256 {e.sha256.slice(0, 16)}
                </code>
              </li>
            ))}
          </ul>
        </div>
      )}

      <Panel title={t("payablesTitle")} data-aging-panel>
        {data.aging.length === 0 ? (
          <p className="text-sm text-muted-foreground">{t("noPayables")}</p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <caption className="sr-only">{t("payablesTitle")}</caption>
              <thead className="text-xs text-muted-foreground">
                <tr>
                  <th className="pb-1 pe-3 text-start font-normal">{t("currency")}</th>
                  {buckets.map((b) => (
                    <th key={b} className="pb-1 pe-3 text-start font-normal">
                      {t(`bucket.${b}`)}
                    </th>
                  ))}
                  <th className="pb-1 text-start font-normal">{t("total")}</th>
                </tr>
              </thead>
              <tbody>
                {data.aging.map((row) => (
                  <tr key={row.currency} className="border-t align-top" data-aging-currency={row.currency}>
                    <td className="py-1.5 pe-3 font-mono">{row.currency}</td>
                    {row.buckets.map((b) => (
                      <td key={b.bucket} className="py-1.5 pe-3">
                        <BucketCell bucket={b} currency={row.currency} />
                      </td>
                    ))}
                    <td className="py-1.5">
                      <Money currency={row.currency} amount={row.total} />
                      <span className="block text-xs text-muted-foreground">{t("invoices", { count: row.count })}</span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        {data.unpriced.length > 0 && (
          <div className="text-sm" data-unpriced>
            <p className="text-muted-foreground">{t("unpriced", { count: data.unpriced.length })}</p>
            <ul className="flex flex-wrap gap-2">
              {data.unpriced.map((inv) => (
                <li key={inv.id}>
                  <Link href={`/pay/invoices/${inv.id}`} className="font-mono underline-offset-4 hover:underline" dir="ltr">
                    {inv.reference}
                  </Link>
                </li>
              ))}
            </ul>
          </div>
        )}
      </Panel>

      <Panel title={t("accrualsTitle")} data-accruals>
        <p className="text-sm text-muted-foreground">{t("accrualsIntro")}</p>
        {data.accruals.length === 0 ? (
          <p className="text-sm text-muted-foreground">{t("noAccruals")}</p>
        ) : (
          data.accruals.map((acc) => (
            <div key={acc.currency} className="space-y-1" data-accrual-currency={acc.currency}>
              <p className="text-sm font-medium">
                {t("accrualTotal")} <Money currency={acc.currency} amount={acc.total} />
              </p>
              <ul className="divide-y rounded-lg border text-sm">
                {acc.lines.map((line) => (
                  <li key={`${line.po_id}-${line.line_no}`} className="flex flex-wrap items-center gap-x-3 gap-y-1 px-3 py-2">
                    <Link href={`/pay/purchase-orders#po-${line.po_id}`} className="font-mono underline-offset-4 hover:underline" dir="ltr">
                      {line.po_number}
                    </Link>
                    <bdi>{line.description}</bdi>
                    <span className="text-xs text-muted-foreground">
                      {t("accrualQuantity", { received: line.received, invoiced: line.invoiced, price: line.unit_price })}
                    </span>
                    <span className="ms-auto">
                      <Money currency={acc.currency} amount={line.amount} />
                    </span>
                  </li>
                ))}
              </ul>
            </div>
          ))
        )}
      </Panel>

      <Panel title={t("exceptionsTitle")} data-month-end-exceptions>
        <p className="text-sm text-muted-foreground">{t("exceptionsIntro")}</p>
        {data.exceptions_count === 0 ? (
          <p className="text-sm text-muted-foreground">{t("noExceptions")}</p>
        ) : (
          <>
            <ul className="flex flex-wrap gap-2 text-sm">
              {data.exception_groups.map((g) => (
                <li key={g.category}>
                  <Link
                    href={`/pay/exceptions?category=${g.category}`}
                    className="inline-flex items-center gap-1 rounded-md border px-2 py-1 hover:bg-accent"
                    data-exception-group={g.category}
                  >
                    {CATEGORIES.has(g.category) ? t(`category.${g.category}` as "category.other") : g.category}
                    <span className="font-mono tabular-nums">{g.count}</span>
                  </Link>
                </li>
              ))}
            </ul>
            <ul className="divide-y rounded-lg border text-sm">
              {data.exceptions.map((e) => (
                <li key={e.id} className="flex flex-wrap items-center gap-x-3 px-3 py-2">
                  <Link href={`/pay/invoices/${e.id}`} className="font-mono underline-offset-4 hover:underline" dir="ltr">
                    {e.reference}
                  </Link>
                  <bdi className="text-muted-foreground">{e.vendor_name ?? ""}</bdi>
                  {e.total && e.currency && (
                    <span className="ms-auto">
                      <Money currency={e.currency} amount={e.total} />
                    </span>
                  )}
                </li>
              ))}
            </ul>
          </>
        )}
      </Panel>
    </div>
  );
}
