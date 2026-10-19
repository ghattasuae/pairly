// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
"use client";

import { Banknote, Download, Eye, Loader2, Send, ShieldAlert } from "lucide-react";
import { useFormatter, useTranslations } from "next-intl";
import { useId, useState } from "react";
import type * as React from "react";

import { EmptyState } from "@/components/states/empty-state";
import { StepUpPrompt } from "@/components/states/step-up-prompt";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Link, useRouter } from "@/i18n/navigation";
import { previewRun, proposeRun, runAction } from "@/lib/actions/pay";
import { RUN_STATUSES, runSums, type RunStatus } from "@/lib/pay/payments";
import type { PayRunDetail, PayRunExcluded, PayRunPreview, PayRunSummary } from "@/lib/pay/types";

import { ErrorText, Panel } from "./common";
import { ReasonText } from "./invoice-approval";
import { chipClass, TABLE } from "./ledger";

export function RunStatusBadge({ status }: { status: string }) {
  const t = useTranslations("Pay.runStatus");
  const variant =
    status === "awaiting_approval"
      ? "info"
      : status === "paid"
        ? "success"
        : status === "approved" || status === "exported"
          ? "default"
          : status === "cancelled" || status === "rejected"
            ? "danger"
            : "outline";
  return (
    <Badge variant={variant} data-run-status={status}>
      {t(status as "paid")}
    </Badge>
  );
}

function Money({ currency, amount }: { currency: string; amount: string | number }) {
  return (
    <span className="font-mono tabular-nums" dir="ltr" data-amount>
      {currency} {String(amount)}
    </span>
  );
}

function ExcludedList({ items }: { items: PayRunExcluded[] }) {
  const t = useTranslations("Pay.runs");
  if (items.length === 0) return null;
  return (
    <div className="space-y-2" data-excluded>
      <h3 className="text-sm font-medium">{t("excluded", { count: items.length })}</h3>
      <ul className="divide-y divide-border rounded-lg border border-border text-sm">
        {items.map((item) => (
          <li key={item.invoice_id} className="space-y-1 px-3 py-2" data-excluded-invoice={item.reference ?? item.invoice_id}>
            <Link href={`/pay/invoices/${item.invoice_id}`} className="font-mono font-medium underline-offset-4 hover:underline">
              {item.reference ?? item.invoice_id}
            </Link>
            {item.amount && <span className="ms-2 font-mono text-muted-foreground">{item.amount}</span>}
            {item.saving && item.pay_by && (
              <p className="text-success-ink" data-saving={item.saving}>
                {t("saving", { amount: item.saving, when: item.pay_by })}
              </p>
            )}
            <ul className="space-y-0.5 text-muted-foreground">
              {item.reasons.map((reason) => (
                <li key={reason}>
                  <ReasonText raw={reason} />
                </li>
              ))}
            </ul>
          </li>
        ))}
      </ul>
    </div>
  );
}

function today(): string {
  return new Date().toISOString().slice(0, 10);
}

/**
 * Payment runs (brief 4.6.3): the list, and the proposal form with a preview of what
 * the run would pay and what it would leave out, and why. A finance controller then
 * approves the run in the inbox. No bank connection: a run ends in a file.
 */
export function PaymentRunList({
  items,
  counts,
  status,
  canCreate,
  defaultCashLimit,
}: {
  items: PayRunSummary[];
  counts: Record<string, number>;
  status: RunStatus | null;
  canCreate: boolean;
  defaultCashLimit: string | null;
}) {
  const t = useTranslations("Pay.runs");
  const tStatus = useTranslations("Pay.runStatus");
  const format = useFormatter();
  const router = useRouter();
  const ids = useId();
  const [form, setForm] = useState({
    currency: "AED",
    cash_limit: defaultCashLimit ?? "",
    execution_date: today(),
    due_on_or_before: "",
    include_early_payment: false,
  });
  const [preview, setPreview] = useState<PayRunPreview | null>(null);
  const [busy, setBusy] = useState<"preview" | "propose" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const total = Object.values(counts).reduce((a, b) => a + b, 0);
  const chip = chipClass;

  function set(patch: Partial<typeof form>) {
    setForm((f) => ({ ...f, ...patch }));
    setPreview(null);
  }

  async function onPreview(event: React.FormEvent) {
    event.preventDefault();
    setBusy("preview");
    setError(null);
    const result = await previewRun(form);
    setBusy(null);
    if (!result.ok) return setError(result.code);
    setPreview(result.preview);
  }

  async function onPropose() {
    setBusy("propose");
    setError(null);
    const result = await proposeRun(form);
    setBusy(null);
    if (!result.ok) return setError(result.code);
    router.push(`/pay/payment-runs/${result.run.id}`);
  }

  return (
    <div className="space-y-6">
      {canCreate && (
        <Panel title={t("proposeTitle")} data-propose>
          <p className="text-sm text-muted-foreground">{t("proposeHelp")}</p>
          <form className="grid gap-3 sm:grid-cols-4 sm:items-end" onSubmit={onPreview}>
            <div className="space-y-1">
              <Label htmlFor={`${ids}-currency`}>{t("currency")}</Label>
              <Input
                id={`${ids}-currency`}
                dir="ltr"
                required
                pattern="[A-Z]{3}"
                maxLength={3}
                value={form.currency}
                onChange={(e) => set({ currency: e.target.value.toUpperCase() })}
              />
            </div>
            <div className="space-y-1">
              <Label htmlFor={`${ids}-limit`}>{t("cashLimit")}</Label>
              <Input
                id={`${ids}-limit`}
                dir="ltr"
                required
                inputMode="decimal"
                pattern="\d{1,13}(\.\d{1,4})?"
                value={form.cash_limit}
                onChange={(e) => set({ cash_limit: e.target.value.trim() })}
              />
            </div>
            <div className="space-y-1">
              <Label htmlFor={`${ids}-date`}>{t("executionDate")}</Label>
              <Input
                id={`${ids}-date`}
                type="date"
                required
                value={form.execution_date}
                onChange={(e) => set({ execution_date: e.target.value })}
              />
            </div>
            <div className="space-y-1">
              <Label htmlFor={`${ids}-due`}>{t("dueBy")}</Label>
              <Input id={`${ids}-due`} type="date" value={form.due_on_or_before} onChange={(e) => set({ due_on_or_before: e.target.value })} />
            </div>
            <div className="flex items-start gap-2 sm:col-span-4">
              <input
                id={`${ids}-early`}
                type="checkbox"
                className="mt-1"
                checked={form.include_early_payment}
                onChange={(e) => set({ include_early_payment: e.target.checked })}
                aria-describedby={`${ids}-early-help`}
              />
              <div className="space-y-0.5">
                <Label htmlFor={`${ids}-early`}>{t("includeEarly")}</Label>
                <p id={`${ids}-early-help`} className="text-xs text-muted-foreground">
                  {t("includeEarlyHelp")}
                </p>
              </div>
            </div>
            <div className="sm:col-span-4">
              <Button type="submit" size="sm" variant="outline" disabled={busy !== null}>
                {busy === "preview" ? <Loader2 aria-hidden className="animate-spin" /> : <Eye aria-hidden />}
                {t("preview")}
              </Button>
            </div>
          </form>
          {preview && (
            <div className="space-y-3" data-preview>
              <p className="text-sm" aria-live="polite">
                {t("previewSummary", { count: preview.items.length })} <Money currency={preview.currency} amount={preview.total} />
              </p>
              {preview.items.length > 0 && (
                <ul className="divide-y divide-border rounded-lg border border-border text-sm" data-preview-items>
                  {preview.items.map((item) => (
                    <li key={String(item.invoice_id)} className="flex flex-wrap justify-between gap-2 px-3 py-2">
                      <span>
                        <span className="font-mono">{String(item.reference)}</span>{" "}
                        <bdi className="text-muted-foreground">{String(item.vendor_name ?? "")}</bdi>
                      </span>
                      <span className="text-muted-foreground">
                        {item.due_date ? t("due", { when: String(item.due_date) }) : t("noDue")} ·{" "}
                        <Money currency={preview.currency} amount={String(item.amount)} />
                        {item.discount && <> · {t("discountTaken", { amount: item.discount })}</>}
                      </span>
                      {!item.discount && item.saving && item.pay_by && (
                        <span className="w-full text-xs text-success-ink" data-saving={item.saving}>
                          {t("saving", { amount: item.saving, when: item.pay_by })}
                        </span>
                      )}
                    </li>
                  ))}
                </ul>
              )}
              {Number(preview.savings) > 0 && (
                <p className="text-sm font-medium text-success-ink" data-savings={String(preview.savings)}>
                  {t("savingsTaken", { amount: `${preview.currency} ${String(preview.savings)}` })}
                </p>
              )}
              {!preview.include_early_payment && Number(preview.savings_available) > 0 && (
                <p className="text-sm" data-savings-available={String(preview.savings_available)}>
                  {t("savingsAvailable", {
                    amount: `${preview.currency} ${String(preview.savings_available)}`,
                  })}
                </p>
              )}
              <ExcludedList items={preview.excluded} />
              <p className="text-xs text-muted-foreground">{t("discountNote")}</p>
              <Button type="button" size="sm" disabled={busy !== null || preview.items.length === 0} onClick={() => void onPropose()}>
                {busy === "propose" ? <Loader2 aria-hidden className="animate-spin" /> : <Send aria-hidden />}
                {t("propose")}
              </Button>
            </div>
          )}
          <ErrorText code={error} />
        </Panel>
      )}

      <nav aria-label={t("filter")} className="flex flex-wrap gap-2" data-run-filter={status ?? "all"}>
        <Link href="/pay/payment-runs" aria-current={status === null ? "page" : undefined} className={chip(status === null)}>
          {t("all")} <span className="tabular-nums text-muted-foreground">{total}</span>
        </Link>
        {RUN_STATUSES.filter((s) => (counts[s] ?? 0) > 0 || s === status).map((s) => (
          <Link key={s} href={`/pay/payment-runs?status=${s}`} aria-current={status === s ? "page" : undefined} className={chip(status === s)}>
            {tStatus(s)} <span className="tabular-nums text-muted-foreground">{counts[s] ?? 0}</span>
          </Link>
        ))}
      </nav>

      {items.length === 0 ? (
        <EmptyState icon={<Banknote aria-hidden />} title={t("emptyTitle")} body={t("emptyBody")} />
      ) : (
        <ul className="card-hairline divide-y divide-border" data-run-list>
          {items.map((run) => (
            <li key={run.id}>
              <Link
                href={`/pay/payment-runs/${run.id}`}
                className="flex flex-wrap items-center justify-between gap-x-6 gap-y-2 px-4 py-3.5 hover:bg-accent sm:px-5"
                data-run={run.reference}
              >
                <span className="space-y-0.5">
                  <span className="block font-mono font-medium">{run.reference}</span>
                  <span className="block text-xs text-muted-foreground">
                    {t("invoices", { count: run.item_count })} ·{" "}
                    {t("payOn", { when: format.dateTime(new Date(run.execution_date), { dateStyle: "medium" }) })}
                  </span>
                </span>
                <span className="flex items-center gap-4">
                  <Money currency={run.currency} amount={run.total} />
                  <RunStatusBadge status={run.status} />
                </span>
              </Link>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

type Download = (runId: string, format: "csv" | "pain001") => Promise<{ ok: true } | { ok: false; code: string; problems: string[] }>;

/** Fetch the bank file through our own route and save it. The browser never sees a token. */
async function defaultDownload(runId: string, format: "csv" | "pain001") {
  const response = await fetch(`/api/pay/payment-runs/${runId}/export?format=${format}`, {
    method: "POST",
    headers: { "X-Pairly-Export": "1" },
    credentials: "same-origin",
  });
  if (!response.ok) {
    let code = "unavailable";
    let problems: string[] = [];
    try {
      const body = (await response.json()) as { code?: string; problems?: string[] };
      code = body.code ?? code;
      problems = Array.isArray(body.problems) ? body.problems : [];
    } catch {
      /* not JSON */
    }
    return { ok: false as const, code, problems };
  }
  const disposition = response.headers.get("content-disposition") ?? "";
  const name = /filename="([^"]+)"/.exec(disposition)?.[1] ?? `payment-run.${format === "csv" ? "csv" : "xml"}`;
  const blob = await response.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  document.body.appendChild(a);
  a.click();
  a.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
  return { ok: true as const };
}

/**
 * One payment run: its invoices, what it left out and why, its approval, the bank file
 * downloads (checked again by the API each time; a change since the approval refuses
 * the file and says what changed), marking paid, and cancelling.
 */
export function PaymentRunView({
  initial,
  totpEnabled,
  download = defaultDownload,
}: {
  initial: PayRunDetail;
  totpEnabled: boolean;
  download?: Download;
}) {
  const t = useTranslations("Pay.runs");
  const format = useFormatter();
  const ids = useId();
  const [run, setRun] = useState(initial);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [problems, setProblems] = useState<string[]>([]);
  const [stepUp, setStepUp] = useState(false);
  const [reference, setReference] = useState("");
  const [why, setWhy] = useState("");
  const [cancelling, setCancelling] = useState(false);
  const sums = runSums(run.items.map((i) => ({ status: i.status, amount: i.amount })));

  async function onDownload(fmt: "csv" | "pain001") {
    setBusy(fmt);
    setError(null);
    setProblems([]);
    setStepUp(false);
    const result = await download(run.id, fmt);
    setBusy(null);
    if (result.ok) return;
    if (result.code === "second_factor_required") return setStepUp(true);
    setError(result.code);
    setProblems(result.problems);
  }

  async function act(action: "paid" | "cancel", text: string) {
    setBusy(action);
    setError(null);
    const result = await runAction(run.id, action, text);
    setBusy(null);
    if (!result.ok) {
      if (result.code === "second_factor_required") setStepUp(true);
      return setError(result.code);
    }
    setRun(result.run);
    setReference("");
    setWhy("");
    setCancelling(false);
  }

  return (
    <div className="space-y-6" data-run-view={run.status}>
      <section className="card-hairline space-y-3 p-5 sm:p-6">
        <div className="flex flex-wrap items-center gap-2">
          <RunStatusBadge status={run.status} />
          <span className="font-mono text-sm text-muted-foreground">
            {t("payOn", { when: format.dateTime(new Date(run.execution_date), { dateStyle: "medium" }) })}
          </span>
        </div>
        <p className="figure text-5xl leading-none" dir="ltr" data-amount>
          {run.currency} {String(run.total)}
        </p>
        <p className="text-sm text-muted-foreground">
          {t("limitLine", { count: run.item_count })} <Money currency={run.currency} amount={run.cash_limit} />
        </p>
        {run.status === "exported" && (
          <p className="text-sm">
            {t("unpaid")} <Money currency={run.currency} amount={sums.unpaid} /> · {t("paid")}{" "}
            <Money currency={run.currency} amount={sums.paid} />
          </p>
        )}
        {run.close_reason && (
          <p className="text-sm text-muted-foreground" data-close-reason={run.close_reason}>
            {t("closed")} <ReasonText raw={run.close_reason} />
          </p>
        )}
        {run.status === "awaiting_approval" && run.approval_id && (
          <p className="text-sm">
            {t("awaiting")}{" "}
            {run.can_approve && (
              <Link href={`/inbox/${run.approval_id}`} className="link-inline" data-open-approval>
                {t("openInInbox")}
              </Link>
            )}
          </p>
        )}
        <p className="text-xs text-muted-foreground">{t("noBank")}</p>
      </section>

      <Panel title={t("items", { count: run.items.length })}>
        <div className={TABLE.wrap}>
          <table className={TABLE.table} data-run-items>
            <thead>
              <tr className={TABLE.headRow}>
                <th className={TABLE.th}>{t("col.invoice")}</th>
                <th className={TABLE.th}>{t("col.vendor")}</th>
                <th className={TABLE.th}>{t("col.due")}</th>
                <th className={TABLE.th}>{t("col.account")}</th>
                <th className={TABLE.thEnd}>{t("col.amount")}</th>
                <th className={TABLE.th}>{t("col.status")}</th>
              </tr>
            </thead>
            <tbody>
              {run.items.map((item) => (
                <tr key={item.id} className={TABLE.row} data-run-item={item.invoice_reference ?? item.invoice_id}>
                  <td className={TABLE.td}>
                    <Link href={`/pay/invoices/${item.invoice_id}`} className="font-mono underline-offset-4 hover:underline">
                      {item.invoice_reference ?? item.invoice_id}
                    </Link>
                    {item.invoice_number && <bdi className="block font-mono text-xs text-muted-foreground">{item.invoice_number}</bdi>}
                  </td>
                  <td className={TABLE.td}>
                    <bdi>{item.vendor_name ?? ""}</bdi>
                  </td>
                  <td className={`${TABLE.td} font-mono text-xs`}>{item.due_date ?? t("noDue")}</td>
                  <td className={`${TABLE.td} font-mono text-xs`} dir="ltr">
                    {item.bank_account_masked ?? t("accountChanged")}
                  </td>
                  <td className={`${TABLE.td} text-end`}>
                    <Money currency={item.currency} amount={item.amount} />
                    {item.discount !== null && item.discount !== undefined && (
                      <span className="block text-xs text-success-ink" data-discount={String(item.discount)}>
                        {t("discountTaken", { amount: `${item.currency} ${String(item.discount)}` })}
                      </span>
                    )}
                  </td>
                  <td className={TABLE.td}>
                    {t(`itemStatus.${item.status}` as "itemStatus.paid")}
                    {item.payment_reference && (
                      <span className="ms-1 font-mono text-xs text-muted-foreground" dir="ltr">
                        {item.payment_reference}
                      </span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <ExcludedList items={run.excluded} />
      </Panel>

      {(run.can_export || run.can_mark_paid || run.can_cancel || run.exports.length > 0) && (
        <Panel title={t("fileTitle")} data-run-actions>
          {run.can_export && (
            <div className="space-y-2">
              <p className="text-sm text-muted-foreground">{t("fileHelp")}</p>
              <div className="flex flex-wrap gap-2">
                <Button type="button" size="sm" variant="outline" disabled={busy !== null} onClick={() => void onDownload("csv")}>
                  {busy === "csv" ? <Loader2 aria-hidden className="animate-spin" /> : <Download aria-hidden />}
                  {t("downloadCsv")}
                </Button>
                <Button type="button" size="sm" variant="outline" disabled={busy !== null} onClick={() => void onDownload("pain001")}>
                  {busy === "pain001" ? <Loader2 aria-hidden className="animate-spin" /> : <Download aria-hidden />}
                  {t("downloadXml")}
                </Button>
              </div>
            </div>
          )}
          {stepUp && <StepUpPrompt totpEnabled={totpEnabled} returnPath={`/pay/payment-runs/${run.id}`} compact />}
          {problems.length > 0 && (
            <div role="alert" className="space-y-1 text-sm" data-export-problems>
              <p className="flex items-center gap-2 font-medium">
                <ShieldAlert aria-hidden className="size-4 text-warning-ink" />
                {t("refused")}
              </p>
              <ul className="space-y-0.5">
                {problems.map((p) => (
                  <li key={p}>
                    <ReasonText raw={p} />
                  </li>
                ))}
              </ul>
            </div>
          )}
          {run.exports.length > 0 && (
            <ul className="space-y-1 text-xs text-muted-foreground" data-exports>
              {run.exports.map((e, i) => (
                <li key={i}>
                  {t("exportLine", { format: String(e.format) === "pain001" ? "pain.001" : "CSV", count: Number(e.items ?? 0) })}{" "}
                  <span className="font-mono" dir="ltr">
                    {String(e.sha256 ?? "").slice(0, 16)}
                  </span>
                </li>
              ))}
            </ul>
          )}
          {run.can_mark_paid && (
            <form
              className="flex flex-wrap items-end gap-2"
              onSubmit={(e) => {
                e.preventDefault();
                void act("paid", reference);
              }}
            >
              <div className="min-w-0 flex-1 space-y-1">
                <Label htmlFor={`${ids}-reference`}>{t("bankReference")}</Label>
                <Input
                  id={`${ids}-reference`}
                  dir="ltr"
                  required
                  maxLength={64}
                  pattern="[\w./:#-]{1,64}"
                  value={reference}
                  onChange={(e) => setReference(e.target.value)}
                />
              </div>
              <Button type="submit" size="sm" disabled={!reference.trim() || busy !== null}>
                {busy === "paid" && <Loader2 aria-hidden className="animate-spin" />}
                {t("markPaid")}
              </Button>
            </form>
          )}
          {run.can_cancel && (
            <div className="space-y-2">
              <Button type="button" size="sm" variant="ghost" onClick={() => setCancelling((c) => !c)}>
                {t("cancel")}
              </Button>
              {cancelling && (
                <form
                  className="space-y-2"
                  onSubmit={(e) => {
                    e.preventDefault();
                    void act("cancel", why);
                  }}
                >
                  <Label htmlFor={`${ids}-why`}>{t("cancelReason")}</Label>
                  <Input id={`${ids}-why`} required maxLength={1000} value={why} onChange={(e) => setWhy(e.target.value)} />
                  <Button type="submit" size="sm" variant="destructive" disabled={!why.trim() || busy !== null}>
                    {t("confirmCancel")}
                  </Button>
                </form>
              )}
            </div>
          )}
          <ErrorText code={error} />
        </Panel>
      )}
    </div>
  );
}
