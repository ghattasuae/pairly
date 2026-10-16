// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
"use client";

import { FileUp, Loader2, RefreshCw } from "lucide-react";
import { useFormatter, useTranslations } from "next-intl";
import { useId, useState } from "react";
import type * as React from "react";

import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Link, useRouter } from "@/i18n/navigation";
import { importStatement, matchStatementsNow, type PayImportProblem } from "@/lib/actions/pay";
import { statementFormat } from "@/lib/pay/reconciliation";
import type { PayReconciliation, PayStatementLine, PayUnexplained } from "@/lib/pay/types";

import { ErrorText, Panel } from "./common";

const PROBLEM_CODES = new Set([
  "invalid_date",
  "invalid_amount",
  "invalid_currency",
  "invalid_iban",
  "invalid_indicator",
  "too_long",
  "required",
  "missing_column",
  "unknown_column",
  "formula_not_allowed",
  "malformed_csv",
  "malformed_xml",
  "not_camt053",
  "no_rows",
  "too_large",
]);
const REASONS = new Set(["amount", "reference", "iban"]);

function Money({ currency, amount }: { currency: string; amount: string }) {
  return (
    <span className="font-mono tabular-nums" dir="ltr">
      {currency} {amount}
    </span>
  );
}

function Day({ iso }: { iso: string }) {
  const format = useFormatter();
  return <>{format.dateTime(new Date(`${iso.slice(0, 10)}T00:00:00Z`), { dateStyle: "medium", timeZone: "UTC" })}</>;
}

function Problem({ problem }: { problem: PayImportProblem }) {
  const t = useTranslations("Pay.reconciliation");
  const what = PROBLEM_CODES.has(problem.code) ? t(`problem.${problem.code}` as "problem.required") : problem.code;
  return (
    <li data-statement-problem={`${problem.row}:${problem.code}`}>
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

/** Upload a CSV or camt.053 statement; nothing is imported when a line has a problem. */
function ImportPanel() {
  const t = useTranslations("Pay.reconciliation");
  const router = useRouter();
  const ids = useId();
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
    const result = await importStatement(statementFormat(file.name), await file.text());
    setBusy(false);
    if (!result.ok) {
      setError(result.code);
      setProblems(result.problems);
      return;
    }
    setDone(t("imported", { reference: result.reference, lines: result.lines, matched: result.matched }));
    router.refresh();
  }

  async function onMatch() {
    setBusy(true);
    setError(null);
    setDone(null);
    const result = await matchStatementsNow();
    setBusy(false);
    if (!result.ok) {
      setError(result.code);
      return;
    }
    setDone(t("matchedNow", { count: result.matched }));
    router.refresh();
  }

  return (
    <Panel title={t("importTitle")} data-statement-import>
      <p className="text-sm text-muted-foreground">{t("format")}</p>
      <form className="flex flex-wrap items-end gap-3" onSubmit={onSubmit}>
        <div className="space-y-1">
          <Label htmlFor={`${ids}-file`}>{t("file")}</Label>
          <input
            id={`${ids}-file`}
            type="file"
            accept=".csv,text/csv,.xml,application/xml,text/xml"
            className="block text-sm"
            onChange={(e) => setFile(e.target.files?.[0] ?? null)}
          />
        </div>
        <Button type="submit" size="sm" disabled={busy || !file}>
          {busy ? <Loader2 aria-hidden className="animate-spin" /> : <FileUp aria-hidden />}
          {t("import")}
        </Button>
        <Button type="button" size="sm" variant="outline" disabled={busy} onClick={onMatch} data-match-now>
          <RefreshCw aria-hidden />
          {t("matchNow")}
        </Button>
      </form>
      {done && (
        <p role="status" className="text-sm" data-statement-done>
          {done}
        </p>
      )}
      <ErrorText code={error} />
      {problems.length > 0 && (
        <ul className="list-disc space-y-0.5 ps-5 text-sm text-destructive" data-statement-problems>
          {problems.map((p, i) => (
            <Problem key={`${p.row}-${p.field ?? ""}-${p.code}-${i}`} problem={p} />
          ))}
        </ul>
      )}
    </Panel>
  );
}

function Target({ line }: { line: PayStatementLine }) {
  const t = useTranslations("Pay.reconciliation");
  return (
    <span className="flex flex-wrap gap-x-2">
      {line.invoice_id && line.invoice_reference && (
        <Link href={`/pay/invoices/${line.invoice_id}`} className="font-mono underline-offset-4 hover:underline" dir="ltr">
          {line.invoice_reference}
        </Link>
      )}
      {line.payment_run_id && line.payment_run_reference && (
        <Link href={`/pay/payment-runs/${line.payment_run_id}`} className="font-mono underline-offset-4 hover:underline" dir="ltr">
          {line.payment_run_reference}
        </Link>
      )}
      {line.match_kind === "run" && <span className="text-xs text-muted-foreground">{t("wholeRun")}</span>}
    </span>
  );
}

function LineRow({ line, matched }: { line: PayStatementLine; matched: boolean }) {
  const t = useTranslations("Pay.reconciliation");
  return (
    <tr className="border-t align-top" data-statement-line={line.status}>
      <td className="py-1.5 pe-3">
        <Day iso={line.booking_date} />
      </td>
      <td className="py-1.5 pe-3">
        <Money currency={line.currency} amount={line.amount} />
      </td>
      <td className="py-1.5 pe-3">
        <bdi>{line.reference ?? "-"}</bdi>
        {line.counterparty_iban_masked && (
          <span className="block font-mono text-xs text-muted-foreground" dir="ltr">
            {line.counterparty_iban_masked}
          </span>
        )}
      </td>
      <td className="py-1.5 pe-3 font-mono text-xs" dir="ltr">
        {line.statement_reference}
      </td>
      <td className="py-1.5">
        {matched ? (
          <>
            <Target line={line} />
            <span className="block text-xs text-muted-foreground">
              {line.match_reasons
                .filter((r) => REASONS.has(r))
                .map((r) => t(`reason.${r}` as "reason.amount"))
                .join(", ")}
            </span>
          </>
        ) : (
          <span className="text-xs" data-unmatched-reason={line.unmatched_reason ?? ""}>
            {line.unmatched_reason === "ambiguous" ? t("ambiguous") : t("noCandidate")}
          </span>
        )}
      </td>
    </tr>
  );
}

function LineTable({ lines, matched, caption }: { lines: PayStatementLine[]; matched: boolean; caption: string }) {
  const t = useTranslations("Pay.reconciliation");
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <caption className="sr-only">{caption}</caption>
        <thead className="text-start text-xs text-muted-foreground">
          <tr>
            <th className="pb-1 pe-3 text-start font-normal">{t("col.date")}</th>
            <th className="pb-1 pe-3 text-start font-normal">{t("col.amount")}</th>
            <th className="pb-1 pe-3 text-start font-normal">{t("col.reference")}</th>
            <th className="pb-1 pe-3 text-start font-normal">{t("col.statement")}</th>
            <th className="pb-1 text-start font-normal">{matched ? t("col.matchedWith") : t("col.why")}</th>
          </tr>
        </thead>
        <tbody>
          {lines.map((line) => (
            <LineRow key={line.id} line={line} matched={matched} />
          ))}
        </tbody>
      </table>
    </div>
  );
}

function UnexplainedTable({ rows }: { rows: PayUnexplained[] }) {
  const t = useTranslations("Pay.reconciliation");
  return (
    <ul className="divide-y rounded-lg border text-sm" data-unexplained>
      {rows.map((row) => (
        <li key={row.invoice_id} className="flex flex-wrap items-center gap-x-3 gap-y-1 px-3 py-2">
          <Link href={`/pay/invoices/${row.invoice_id}`} className="font-mono underline-offset-4 hover:underline" dir="ltr">
            {row.invoice_reference}
          </Link>
          <bdi className="text-muted-foreground">{row.vendor_name ?? ""}</bdi>
          <Money currency={row.currency} amount={row.amount} />
          <Link href={`/pay/payment-runs/${row.payment_run_id}`} className="font-mono text-xs underline-offset-4 hover:underline" dir="ltr">
            {row.payment_run_reference}
          </Link>
          <span className="ms-auto text-xs text-muted-foreground">{t("paidAgo", { days: row.days_since_paid })}</span>
        </li>
      ))}
    </ul>
  );
}

/**
 * Bank reconciliation: statement lines matched with paid invoices and payment runs,
 * the debits nothing explains, and the paid invoices no statement line explains after
 * the chosen number of days. Every row links to its invoice, run or statement.
 */
export function PayReconciliationView({ data }: { data: PayReconciliation }) {
  const t = useTranslations("Pay.reconciliation");
  return (
    <div className="space-y-6" data-reconciliation>
      {data.can_upload && <ImportPanel />}
      <dl className="grid gap-3 sm:grid-cols-4" data-reconciliation-counts>
        {(
          [
            ["matched", data.matched_count],
            ["unmatched", data.unmatched_count],
            ["unexplained", data.unexplained_count],
            ["ignored", data.ignored_count],
          ] as const
        ).map(([key, value]) => (
          <div key={key} className="rounded-lg border p-3">
            <dt className="text-xs text-muted-foreground">{t(`count.${key}`, { days: data.days })}</dt>
            <dd className="font-display text-2xl tabular-nums" data-count={key}>
              <a href={`#${key}`} className="hover:underline">
                {value}
              </a>
            </dd>
          </div>
        ))}
      </dl>

      <Panel title={t("unmatchedTitle")} data-unmatched-lines>
        <span id="unmatched" />
        {data.unmatched.length === 0 ? (
          <p className="text-sm text-muted-foreground">{t("noUnmatched")}</p>
        ) : (
          <LineTable lines={data.unmatched} matched={false} caption={t("unmatchedTitle")} />
        )}
      </Panel>

      <Panel title={t("unexplainedTitle", { days: data.days })} data-unexplained-panel>
        <span id="unexplained" />
        <p className="text-sm text-muted-foreground">{t("unexplainedIntro", { days: data.days })}</p>
        {data.unexplained.length === 0 ? (
          <p className="text-sm text-muted-foreground">{t("noUnexplained")}</p>
        ) : (
          <UnexplainedTable rows={data.unexplained} />
        )}
      </Panel>

      <Panel title={t("matchedTitle")} data-matched-lines>
        <span id="matched" />
        {data.matched.length === 0 ? (
          <p className="text-sm text-muted-foreground">{t("noMatched")}</p>
        ) : (
          <LineTable lines={data.matched} matched caption={t("matchedTitle")} />
        )}
      </Panel>

      <Panel title={t("statementsTitle")} data-statements>
        <span id="ignored" />
        {data.statements.length === 0 ? (
          <p className="text-sm text-muted-foreground">{t("noStatements")}</p>
        ) : (
          <ul className="divide-y rounded-lg border text-sm">
            {data.statements.map((s) => (
              <li key={s.id} className="flex flex-wrap items-center gap-x-3 gap-y-1 px-3 py-2" data-statement={s.reference}>
                <span className="font-mono" dir="ltr">
                  {s.reference}
                </span>
                <span className="text-xs uppercase text-muted-foreground">{s.format === "camt053" ? "camt.053" : "CSV"}</span>
                {s.account_masked && (
                  <span className="font-mono text-xs text-muted-foreground" dir="ltr">
                    {s.account_masked}
                  </span>
                )}
                <span className="text-xs text-muted-foreground">
                  <Day iso={s.from_date} /> - <Day iso={s.to_date} />
                </span>
                <span className="ms-auto text-xs">{t("lines", { count: s.line_count })}</span>
              </li>
            ))}
          </ul>
        )}
      </Panel>
    </div>
  );
}
