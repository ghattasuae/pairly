// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
"use client";

import { AlertTriangle, CheckCircle2, CircleSlash, Copy, ShieldAlert } from "lucide-react";
import { useLocale, useTranslations } from "next-intl";
import { useId } from "react";

import { Badge } from "@/components/ui/badge";
import { Link } from "@/i18n/navigation";
import { compareLines, compareRows, failedChecks, findingMessage, sortedFindings } from "@/lib/pay/checks";
import type { PayCheck, PayCompare, PayRisk } from "@/lib/pay/types";
import { cn } from "@/lib/utils";

const LEVELS = new Set(["low", "medium", "high", "critical"]);

/**
 * What the validation and fraud checks found (brief 4.4): the risk and why, every
 * finding in one plain sentence (worst first), and the checks that could not run.
 * Not colour alone: each finding has an icon and a severity word.
 */
export function InvoiceChecks({ checks, risk }: { checks: readonly PayCheck[]; risk: PayRisk | null | undefined }) {
  const t = useTranslations("Pay.checks");
  const locale = useLocale();
  const id = useId();
  if (checks.length === 0 && !risk) return null;
  const findings = sortedFindings(checks).filter((f) => f.severity !== "info" || f.code === "no_policy_found");
  const failed = failedChecks(checks);
  const level = risk && LEVELS.has(risk.level) ? risk.level : "high";
  return (
    <section aria-labelledby={`${id}-title`} className="space-y-3 rounded-xl border bg-surface p-4" data-invoice-checks>
      <h2 id={`${id}-title`} className="eyebrow">
        {t("title")}
      </h2>
      {risk && (
        <div className="flex flex-wrap items-center gap-2" data-risk-level={level}>
          <Badge variant={level === "low" ? "default" : "signal"}>{t(`level.${level}` as "level.low")}</Badge>
          {risk.requires_human && <span className="text-xs text-muted-foreground">{t("needsPerson")}</span>}
          <p className="w-full text-sm">{findingMessage({ message: risk.explanation }, locale)}</p>
        </div>
      )}
      {findings.length === 0 && failed.length === 0 ? (
        <p className="flex items-center gap-2 text-sm text-muted-foreground">
          <CheckCircle2 aria-hidden className="size-4" />
          {t("nothingFound")}
        </p>
      ) : (
        <ul className="space-y-2" aria-label={t("findings")}>
          {findings.map((f, index) => (
            <li
              key={`${f.check}-${f.rule}-${index}`}
              data-finding={f.code}
              data-severity={f.severity}
              className={cn(
                "flex items-start gap-2 rounded-lg border p-2 text-sm",
                f.needs_review && "border-signal bg-signal-soft/40",
              )}
            >
              {f.severity === "info" ? (
                <CircleSlash aria-hidden className="mt-0.5 size-4 shrink-0 text-muted-foreground" />
              ) : f.check === "injection" ? (
                <ShieldAlert aria-hidden className="mt-0.5 size-4 shrink-0 text-danger-ink" />
              ) : (
                <AlertTriangle aria-hidden className="mt-0.5 size-4 shrink-0 text-signal" />
              )}
              <span className="min-w-0 flex-1">
                <span className="block" dir="auto">
                  {findingMessage(f, locale)}
                </span>
                <span className="mt-0.5 flex flex-wrap gap-x-3 text-xs text-muted-foreground">
                  <span>{t(`severity.${f.severity}` as "severity.high")}</span>
                  <span>{t(`check.${f.check}` as "check.bank")}</span>
                  {f.original_id && (
                    <Link href={`/pay/invoices/${f.original_id}`} className="underline-offset-4 hover:underline">
                      {t("openOriginal")}
                    </Link>
                  )}
                </span>
              </span>
            </li>
          ))}
          {failed.map((c) => (
            <li key={`error-${c.check}`} data-check-error={c.check} className="flex items-start gap-2 rounded-lg border border-signal p-2 text-sm">
              <AlertTriangle aria-hidden className="mt-0.5 size-4 shrink-0 text-signal" />
              <span>{t("couldNotRun", { check: t(`check.${c.check}` as "check.bank") })}</span>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

/** A suspected original next to this invoice, differences marked (brief 4.4.2). */
export function ComparePanel({ compare }: { compare: readonly PayCompare[] }) {
  const t = useTranslations("Pay.compare");
  const fields = useTranslations("Pay.fields");
  const id = useId();
  if (compare.length === 0) return null;
  return (
    <section aria-labelledby={`${id}-title`} className="space-y-4 rounded-xl border bg-surface p-4" data-compare-panel>
      <h2 id={`${id}-title`} className="flex items-center gap-2 eyebrow">
        <Copy aria-hidden className="size-4" />
        {t("title")}
      </h2>
      {compare.map((pair) => {
        const rows = compareRows(pair);
        const lines = compareLines(pair);
        return (
          <div key={pair.original.id} className="space-y-3" data-compare={pair.original.id}>
            <p className="text-sm text-muted-foreground">
              {t("intro", { count: rows.filter((r) => !r.same).length })}{" "}
              <Link href={`/pay/invoices/${pair.original.id}`} className="text-primary underline-offset-4 hover:underline">
                {t("openOriginal", { reference: pair.original.reference })}
              </Link>
            </p>
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <caption className="sr-only">{t("caption")}</caption>
                <thead>
                  <tr className="text-start text-xs text-muted-foreground">
                    <th scope="col" className="py-1 text-start font-normal">
                      {t("field")}
                    </th>
                    <th scope="col" className="py-1 text-start font-normal">
                      {t("thisInvoice")}
                    </th>
                    <th scope="col" className="py-1 text-start font-normal">
                      {t("original")}
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((row) => (
                    <tr key={row.field} data-compare-row={row.field} data-same={row.same} className={cn(!row.same && "bg-signal-soft/40")}>
                      <th scope="row" className="py-1 pe-2 text-start font-normal text-muted-foreground">
                        {fields.has(row.field as "total") ? fields(row.field as "total") : row.field}
                      </th>
                      <td className="py-1 pe-2">
                        <bdi>{row.invoice ?? t("empty")}</bdi>
                      </td>
                      <td className="py-1">
                        <bdi>{row.original ?? t("empty")}</bdi>
                        {!row.same && <span className="sr-only"> {t("differs")}</span>}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            {lines.length > 0 && (
              <div className="space-y-1">
                <h3 className="text-sm font-medium">{t("lines")}</h3>
                <ol className="space-y-1 text-sm">
                  {lines.map((line) => (
                    <li
                      key={line.index}
                      data-compare-line={line.index}
                      data-same={line.same}
                      className={cn("grid grid-cols-2 gap-2 rounded border p-1.5", !line.same && "border-signal")}
                    >
                      <span dir="auto">
                        {line.invoice ? `${line.invoice.description ?? ""} ${line.invoice.quantity ?? ""} x ${line.invoice.unit_price ?? ""} = ${line.invoice.line_total ?? ""}` : t("empty")}
                      </span>
                      <span dir="auto">
                        {line.original ? `${line.original.description ?? ""} ${line.original.quantity ?? ""} x ${line.original.unit_price ?? ""} = ${line.original.line_total ?? ""}` : t("empty")}
                      </span>
                    </li>
                  ))}
                </ol>
              </div>
            )}
          </div>
        );
      })}
    </section>
  );
}
