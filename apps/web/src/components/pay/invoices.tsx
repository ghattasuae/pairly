// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
"use client";

import { FileText } from "lucide-react";
import { useFormatter, useTranslations } from "next-intl";

import { EmptyState } from "@/components/states/empty-state";
import { Link } from "@/i18n/navigation";
import { INVOICE_STATUSES, type InvoiceStatus } from "@/lib/pay/invoices";
import type { PayInvoiceSummary } from "@/lib/pay/types";

import { InvoiceStatusBadge } from "./invoice-review";

/** The invoice list (brief 4.2): filter chips by status, newest first. */
export function InvoiceList({
  items,
  counts,
  status,
}: {
  items: PayInvoiceSummary[];
  counts: Record<string, number>;
  status: InvoiceStatus | null;
}) {
  const t = useTranslations("Pay.invoices");
  const tStatus = useTranslations("Pay.invoiceStatus");
  const format = useFormatter();
  const total = Object.values(counts).reduce((a, b) => a + b, 0);
  const chip = (active: boolean) =>
    active
      ? "inline-flex h-8 items-center gap-1 rounded-md bg-secondary px-3 text-[0.8rem] font-medium"
      : "inline-flex h-8 items-center gap-1 rounded-md border px-3 text-[0.8rem] hover:bg-accent";
  return (
    <div className="space-y-4">
      <nav aria-label={t("filter")} className="flex flex-wrap gap-2" data-status-filter={status ?? "all"}>
        <Link href="/pay/invoices" aria-current={status === null ? "page" : undefined} className={chip(status === null)}>
          {t("all")} <span className="tabular-nums text-muted-foreground">{total}</span>
        </Link>
        {INVOICE_STATUSES.filter((s) => (counts[s] ?? 0) > 0 || s === status).map((s) => (
          <Link
            key={s}
            href={`/pay/invoices?status=${s}`}
            aria-current={status === s ? "page" : undefined}
            className={chip(status === s)}
          >
            {tStatus(s)} <span className="tabular-nums text-muted-foreground">{counts[s] ?? 0}</span>
          </Link>
        ))}
      </nav>
      {items.length === 0 ? (
        <EmptyState icon={<FileText aria-hidden />} title={t("emptyTitle")} body={t("emptyBody")} />
      ) : (
        <ul className="divide-y rounded-xl border bg-surface" data-invoice-list>
          {items.map((item) => (
            <li key={item.id}>
              <Link
                href={`/pay/invoices/${item.id}`}
                className="flex flex-wrap items-center justify-between gap-3 px-4 py-3 hover:bg-accent"
                data-invoice={item.reference}
              >
                <span className="min-w-0 space-y-0.5">
                  <span className="block font-medium">
                    <bdi>{item.vendor?.legal_name ?? item.vendor_name ?? t("unknownVendor")}</bdi>
                  </span>
                  <span className="block text-xs text-muted-foreground">
                    <bdi>{item.invoice_number ?? t("noNumber")}</bdi> · {item.reference} · {t(`source.${item.source}` as "source.email")}
                    {item.invoice_date && <> · {format.dateTime(new Date(item.invoice_date), { dateStyle: "medium" })}</>}
                  </span>
                </span>
                <span className="flex items-center gap-3">
                  {item.total && item.currency && (
                    <span className="font-mono text-sm tabular-nums" dir="ltr">
                      {item.currency} {item.total}
                    </span>
                  )}
                  <InvoiceStatusBadge status={item.status} />
                </span>
              </Link>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
