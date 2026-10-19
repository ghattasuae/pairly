// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
"use client";

import { CheckCircle2, Loader2, RefreshCw, Send, ShieldAlert } from "lucide-react";
import { useFormatter, useTranslations } from "next-intl";
import { useId, useState } from "react";
import type * as React from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Link } from "@/i18n/navigation";
import { codeInvoice, invoiceAction } from "@/lib/actions/pay";
import { parseReason, submitState, type ChecksState } from "@/lib/pay/payments";
import type { PayInvoiceDetail } from "@/lib/pay/types";

import { ErrorText } from "./common";

/** A reason from the API in plain words, the detail after the code kept as it is. */
export function ReasonText({ raw }: { raw: string }) {
  const t = useTranslations("Pay.blockers");
  const reason = parseReason(raw);
  if (!reason.code) return <span className="font-mono text-xs" dir="ltr">{raw}</span>;
  return (
    <span data-reason={reason.code}>
      {reason.item && (
        <span className="me-1 font-mono text-xs text-muted-foreground" dir="ltr">
          {reason.item}
        </span>
      )}
      {t(reason.code)}
      {reason.detail && (
        <span className="ms-1 font-mono text-xs text-muted-foreground" dir="ltr">
          ({reason.detail})
        </span>
      )}
    </span>
  );
}

export function ChecksStateBadge({ state }: { state: ChecksState }) {
  const t = useTranslations("Pay.approval.checksState");
  return (
    <Badge variant={state === "current" ? "success" : "warning"} data-checks-state={state}>
      {t(state)}
    </Badge>
  );
}

/**
 * Approval and payment of one invoice (WP4.7): whether the checks are current, the
 * coding the approval rules route on, why it cannot be sent for approval yet, the
 * "send for approval" step, and then where it is: waiting in the inbox, approved (by
 * whom, or by a rule), in a payment run, paid.
 */
export function InvoiceApprovalPanel({
  invoice,
  onChange,
}: {
  invoice: PayInvoiceDetail;
  onChange: (invoice: PayInvoiceDetail) => void;
}) {
  const t = useTranslations("Pay.approval");
  const format = useFormatter();
  const ids = useId();
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState("");
  const [department, setDepartment] = useState(invoice.department ?? "");
  const [category, setCategory] = useState(invoice.category ?? "");
  const open = invoice.status === "needs_review" || invoice.status === "ready";
  const editable = invoice.can_process && open;
  const state = (invoice.checks_state ?? "missing") as ChecksState;
  const submit = submitState({ status: invoice.status, checks_state: state, blockers: invoice.blockers });
  const approval = invoice.approval ?? null;

  async function run(action: "checks" | "submit", text = "") {
    setBusy(action);
    setError(null);
    const result = await invoiceAction(invoice.id, action, text);
    setBusy(null);
    if (!result.ok) return setError(result.code);
    setNote("");
    onChange(result.invoice);
  }

  async function saveCoding(event: React.FormEvent) {
    event.preventDefault();
    setBusy("coding");
    setError(null);
    const result = await codeInvoice(invoice.id, { department, category });
    setBusy(null);
    if (!result.ok) return setError(result.code);
    onChange(result.invoice);
  }

  return (
    <section className="card-hairline space-y-3 p-5" aria-labelledby={`${ids}-title`} data-approval-panel={invoice.status}>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 id={`${ids}-title`} className="eyebrow">
          {t("title")}
        </h2>
        <ChecksStateBadge state={state} />
      </div>

      {state !== "current" && open && (
        <div className="space-y-2 text-sm" data-checks-warning>
          <p className="flex items-start gap-2">
            <ShieldAlert aria-hidden className="mt-0.5 size-4 shrink-0 text-warning-ink" />
            {t(`checksHelp.${state}`)}
          </p>
          {invoice.can_process && (
            <Button type="button" size="sm" variant="outline" disabled={busy !== null} onClick={() => void run("checks")}>
              {busy === "checks" ? <Loader2 aria-hidden className="animate-spin" /> : <RefreshCw aria-hidden />}
              {t("rerun")}
            </Button>
          )}
        </div>
      )}

      {editable && (
        <form className="grid gap-2 sm:grid-cols-[1fr_1fr_auto] sm:items-end" onSubmit={saveCoding} data-coding-form>
          <div className="space-y-1">
            <Label htmlFor={`${ids}-department`}>{t("department")}</Label>
            <Input
              id={`${ids}-department`}
              dir="ltr"
              pattern="[a-z][a-z0-9_]{0,63}"
              placeholder="operations"
              value={department}
              onChange={(e) => setDepartment(e.target.value.toLowerCase())}
            />
          </div>
          <div className="space-y-1">
            <Label htmlFor={`${ids}-category`}>{t("category")}</Label>
            <Input
              id={`${ids}-category`}
              dir="ltr"
              pattern="[a-z][a-z0-9_]{0,63}"
              placeholder={invoice.vendor ? "software" : ""}
              value={category}
              onChange={(e) => setCategory(e.target.value.toLowerCase())}
            />
          </div>
          <Button type="submit" size="sm" variant="outline" disabled={busy !== null}>
            {busy === "coding" && <Loader2 aria-hidden className="animate-spin" />}
            {t("saveCoding")}
          </Button>
          <p className="text-xs text-muted-foreground sm:col-span-3">{t("codingHelp")}</p>
        </form>
      )}

      {invoice.status === "ready" && invoice.can_process && (
        <div className="space-y-2" data-submit>
          {submit.why.length > 0 ? (
            <div className="space-y-1 text-sm" data-blockers>
              <p className="font-medium">{t("blocked")}</p>
              <ul className="space-y-1">
                {submit.why.map((reason) => (
                  <li key={reason} className="flex items-start gap-2">
                    <ShieldAlert aria-hidden className="mt-0.5 size-4 shrink-0 text-warning-ink" />
                    <ReasonText raw={reason} />
                  </li>
                ))}
              </ul>
            </div>
          ) : (
            <form
              className="space-y-2"
              onSubmit={(e) => {
                e.preventDefault();
                void run("submit", note);
              }}
            >
              <Label htmlFor={`${ids}-note`}>{t("note")}</Label>
              <Input id={`${ids}-note`} maxLength={1000} value={note} onChange={(e) => setNote(e.target.value)} />
              <Button type="submit" size="sm" disabled={busy !== null}>
                {busy === "submit" ? <Loader2 aria-hidden className="animate-spin" /> : <Send aria-hidden />}
                {t("submit")}
              </Button>
              <p className="text-xs text-muted-foreground">{t("submitHelp")}</p>
            </form>
          )}
        </div>
      )}

      {invoice.status === "awaiting_approval" && (
        <p className="text-sm" data-awaiting>
          {t("awaiting")}{" "}
          {approval?.approval_id && (
            <Link href={`/inbox/${approval.approval_id}`} className="text-primary underline-offset-4 hover:underline">
              {t("openInInbox")}
            </Link>
          )}
        </p>
      )}

      {approval?.approved_at && ["approved", "scheduled", "paid"].includes(invoice.status) && (
        <p className="flex items-start gap-2 text-sm" data-approved={approval.approved_by_rule ? "rule" : "people"}>
          <CheckCircle2 aria-hidden className="mt-0.5 size-4 shrink-0 text-success-ink" />
          <span>
            {approval.approved_by_rule
              ? t("approvedByRule", { when: format.dateTime(new Date(approval.approved_at), { dateStyle: "medium" }) })
              : t("approvedByPeople", {
                  count: approval.approver_ids.length,
                  when: format.dateTime(new Date(approval.approved_at), { dateStyle: "medium" }),
                })}
          </span>
        </p>
      )}

      {invoice.payment_run_id && (
        <p className="text-sm" data-payment-run>
          <Link href={`/pay/payment-runs/${invoice.payment_run_id}`} className="text-primary underline-offset-4 hover:underline">
            {t("inRun")}
          </Link>
          {invoice.paid_at && (
            <span className="ms-2 text-muted-foreground">
              {t("paidOn", { when: format.dateTime(new Date(invoice.paid_at), { dateStyle: "medium" }) })}
              {invoice.payment_reference && (
                <span className="ms-1 font-mono text-xs" dir="ltr">
                  {invoice.payment_reference}
                </span>
              )}
            </span>
          )}
        </p>
      )}

      <ErrorText code={error} />
    </section>
  );
}
