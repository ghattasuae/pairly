// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
"use client";

import { ClipboardList, FileUp, Loader2 } from "lucide-react";
import { useTranslations } from "next-intl";
import { useId, useState } from "react";
import type * as React from "react";

import { EmptyState } from "@/components/states/empty-state";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { useRouter } from "@/i18n/navigation";
import { importPurchaseCsv, type PayImportProblem } from "@/lib/actions/pay";
import type { PayPoSummary } from "@/lib/pay/types";

import { ErrorText, Panel } from "./common";

const PROBLEM_CODES = new Set([
  "required",
  "invalid_number",
  "invalid_currency",
  "invalid_date",
  "invalid_quantity",
  "invalid_unit_price",
  "invalid_line_no",
  "invalid_match_type",
  "duplicate_line_no",
  "formula_not_allowed",
  "too_long",
  "unknown_column",
  "missing_column",
  "duplicate_column",
  "wrong_column_count",
  "differs_within_po",
  "differs_within_receipt",
  "vendor_not_found",
  "po_not_found",
  "po_line_not_found",
  "more_than_ordered",
  "number_taken",
  "too_large",
  "too_many_rows",
  "no_rows",
  "malformed_csv",
]);

export function PoStatusBadge({ status }: { status: string }) {
  const t = useTranslations("Pay.purchaseOrders.status");
  return (
    <Badge variant={status === "open" ? "default" : "outline"} data-po-status={status}>
      {t(status as "open")}
    </Badge>
  );
}

function Problem({ problem }: { problem: PayImportProblem }) {
  const t = useTranslations("Pay.purchaseOrders");
  const what = PROBLEM_CODES.has(problem.code) ? t(`problem.${problem.code}` as "problem.required") : problem.code;
  return (
    <li data-import-problem={`${problem.row}:${problem.code}`}>
      {problem.row > 0 ? t("problemRow", { row: problem.row }) : t("problemFile")}{" "}
      {problem.field && (
        <code className="font-mono text-xs" dir="ltr">
          {problem.field}
        </code>
      )}{" "}
      {what}
    </li>
  );
}

/** Import POs or goods receipts from a CSV file; nothing is created when a row has a problem. */
function ImportPanel() {
  const t = useTranslations("Pay.purchaseOrders");
  const router = useRouter();
  const ids = useId();
  const [kind, setKind] = useState<"orders" | "receipts">("orders");
  const [file, setFile] = useState<File | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [problems, setProblems] = useState<PayImportProblem[]>([]);
  const [done, setDone] = useState<string | null>(null);

  async function onSubmit(event: React.FormEvent) {
    event.preventDefault();
    if (!file) return;
    setBusy(true);
    setError(null);
    setProblems([]);
    setDone(null);
    const result = await importPurchaseCsv(kind, await file.text());
    setBusy(false);
    if (!result.ok) {
      setError(result.code);
      setProblems(result.problems);
      return;
    }
    setDone(t(kind === "orders" ? "importedOrders" : "importedReceipts", { count: result.created }));
    router.refresh();
  }

  return (
    <Panel title={t("importTitle")} data-po-import>
      <p className="text-sm text-muted-foreground">{t(kind === "orders" ? "ordersFormat" : "receiptsFormat")}</p>
      <form className="flex flex-wrap items-end gap-3" onSubmit={onSubmit}>
        <div className="space-y-1">
          <Label htmlFor={`${ids}-kind`}>{t("importKind")}</Label>
          <select
            id={`${ids}-kind`}
            className="h-9 rounded-md border bg-background px-2 text-sm"
            value={kind}
            onChange={(e) => setKind(e.target.value === "receipts" ? "receipts" : "orders")}
          >
            <option value="orders">{t("kindOrders")}</option>
            <option value="receipts">{t("kindReceipts")}</option>
          </select>
        </div>
        <div className="space-y-1">
          <Label htmlFor={`${ids}-file`}>{t("file")}</Label>
          <input
            id={`${ids}-file`}
            type="file"
            accept=".csv,text/csv"
            className="block text-sm"
            onChange={(e) => setFile(e.target.files?.[0] ?? null)}
          />
        </div>
        <Button type="submit" size="sm" disabled={busy || !file}>
          {busy ? <Loader2 aria-hidden className="animate-spin" /> : <FileUp aria-hidden />}
          {t("import")}
        </Button>
      </form>
      {done && (
        <p role="status" className="text-sm" data-import-done>
          {done}
        </p>
      )}
      <ErrorText code={error} />
      {problems.length > 0 && (
        <ul className="list-disc space-y-0.5 ps-5 text-sm text-destructive" data-import-problems>
          {problems.map((p, i) => (
            <Problem key={`${p.row}-${p.field ?? ""}-${p.code}-${i}`} problem={p} />
          ))}
        </ul>
      )}
    </Panel>
  );
}

/**
 * Purchase orders (brief 4.5.1): read only, with what was received and invoiced, and a
 * CSV import for people who may create them. Invoices are matched against these.
 */
export function PurchaseOrderList({ items, canImport }: { items: PayPoSummary[]; canImport: boolean }) {
  const t = useTranslations("Pay.purchaseOrders");
  return (
    <div className="space-y-6">
      {canImport && <ImportPanel />}
      {items.length === 0 ? (
        <EmptyState icon={<ClipboardList aria-hidden />} title={t("emptyTitle")} body={t("emptyBody")} />
      ) : (
        <div className="overflow-x-auto rounded-xl border bg-surface">
          <table className="w-full text-sm" data-po-list>
            <thead className="text-start text-xs text-muted-foreground">
              <tr className="border-b">
                <th scope="col" className="px-3 py-2 text-start font-medium">{t("col.number")}</th>
                <th scope="col" className="px-3 py-2 text-start font-medium">{t("col.vendor")}</th>
                <th scope="col" className="px-3 py-2 text-start font-medium">{t("col.status")}</th>
                <th scope="col" className="px-3 py-2 text-start font-medium">{t("col.match")}</th>
                <th scope="col" className="px-3 py-2 text-start font-medium">{t("col.ordered")}</th>
                <th scope="col" className="px-3 py-2 text-end font-medium">{t("col.amount")}</th>
                <th scope="col" className="px-3 py-2 text-end font-medium">{t("col.receipts")}</th>
                <th scope="col" className="px-3 py-2 text-end font-medium">{t("col.invoices")}</th>
              </tr>
            </thead>
            <tbody className="divide-y">
              {items.map((po) => (
                <tr key={po.id} data-po={po.po_number}>
                  <td className="px-3 py-2 font-mono" dir="ltr">
                    {po.po_number}
                  </td>
                  <td className="px-3 py-2">
                    <bdi>{po.vendor_name ?? po.vendor_reference ?? ""}</bdi>
                  </td>
                  <td className="px-3 py-2">
                    <PoStatusBadge status={po.status} />
                  </td>
                  <td className="px-3 py-2">{t(po.match_type === "2-way" ? "twoWay" : "threeWay")}</td>
                  <td className="px-3 py-2 tabular-nums" dir="ltr">
                    {po.order_date}
                  </td>
                  <td className="px-3 py-2 text-end font-mono tabular-nums" dir="ltr">
                    {po.currency} {po.subtotal}
                  </td>
                  <td className="px-3 py-2 text-end tabular-nums">{po.receipt_count}</td>
                  <td className="px-3 py-2 text-end tabular-nums">{po.invoice_count}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
