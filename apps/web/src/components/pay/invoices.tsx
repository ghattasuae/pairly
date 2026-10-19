// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
"use client";

import { FileText } from "lucide-react";
import { useFormatter, useTranslations } from "next-intl";

import { EmptyState } from "@/components/states/empty-state";
import { chipClass } from "@/components/ui/chip";
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
  const chip = chipClass;
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
        <ul className="card-hairline divide-y divide-border overflow-hidden" data-invoice-list>
          {items.map((item) => (
            <li key={item.id}>
              <Link
                href={`/pay/invoices/${item.id}`}
                className="flex flex-wrap items-center justify-between gap-x-6 gap-y-2 px-4 py-3.5 hover:bg-accent sm:px-5"
                data-invoice={item.reference}
              >
                <span className="min-w-0 space-y-0.5">
                  <span className="block truncate font-medium">
                    <bdi>{item.vendor?.legal_name ?? item.vendor_name ?? t("unknownVendor")}</bdi>
                  </span>
                  <span className="block text-xs text-muted-foreground">
                    <bdi className="font-mono">{item.invoice_number ?? t("noNumber")}</bdi> ·{" "}
                    <span className="font-mono">{item.reference}</span> · {t(`source.${item.source}` as "source.email")}
                    {item.invoice_date && (
                      <>
                        {" "}
                        · <span className="font-mono">{format.dateTime(new Date(item.invoice_date), { dateStyle: "medium" })}</span>
                      </>
                    )}
                  </span>
                </span>
                <span className="flex items-center gap-4">
                  {item.total && item.currency && (
                    <span className="font-mono text-sm font-medium tabular-nums" dir="ltr" data-amount>
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
