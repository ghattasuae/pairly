// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
"use client";

import { AlertTriangle, ArrowRight, Check, Loader2, Phone, ShieldCheck } from "lucide-react";
import { useFormatter, useTranslations } from "next-intl";
import { useId, useState } from "react";
import type * as React from "react";

import { StepUpPrompt } from "@/components/states/step-up-prompt";
import { Button } from "@/components/ui/button";
import { Checkbox, Textarea } from "@/components/ui/form-controls";
import { Label } from "@/components/ui/label";
import { Link } from "@/i18n/navigation";
import { cancelBankChange, recordCall, startApproval } from "@/lib/actions/pay";
import { CHANGE_STEPS, changeStepIndex, groupIban, isOpenTicket } from "@/lib/pay/format";
import type { PayBankAccount, PayTicketDetail, PayTicketSummary } from "@/lib/pay/types";
import { cn } from "@/lib/utils";

import { ErrorText, HoldBadge, Panel, TicketStatusBadge } from "./common";

type CallResult = "confirmed" | "not_confirmed" | "no_answer";

/** Where the change stands: requested, call back, two approvals, cooling-off, active. */
export function ChangeProgress({ status }: { status: string }) {
  const t = useTranslations("Pay.change.steps");
  const at = changeStepIndex(status);
  return (
    <ol className="flex flex-wrap gap-2 text-xs" data-change-progress={status}>
      {CHANGE_STEPS.map((step, i) => {
        const done = at > i || status === "completed";
        const current = at === i && status !== "completed";
        return (
          <li
            key={step}
            aria-current={current ? "step" : undefined}
            data-step={step}
            data-state={done ? "done" : current ? "current" : "todo"}
            className={cn(
              "inline-flex items-center gap-1 rounded-full border px-2.5 py-1",
              done && "border-transparent bg-secondary",
              current && "border-primary font-medium",
              !done && !current && "text-muted-foreground",
            )}
          >
            {done && <Check aria-hidden className="size-3" />}
            {t(step)}
          </li>
        );
      })}
    </ol>
  );
}

function AccountCard({ title, account, full }: { title: string; account: PayBankAccount | null | undefined; full?: string | null }) {
  const t = useTranslations("Pay.change");
  return (
    <div className="flex-1 space-y-1 rounded-lg border p-3 text-sm">
      <p className="eyebrow">{title}</p>
      {account ? (
        <>
          <p className="font-mono" dir="ltr">
            <bdi>{full ? groupIban(full) : account.iban_masked}</bdi>
          </p>
          <p>{account.account_name}</p>
          <p className="text-muted-foreground">
            {[account.bank_name, account.swift].filter(Boolean).join(" · ")}
            {account.bank_code && (
              <>
                {" "}
                · {t("bankCode")} <bdi dir="ltr">{account.bank_code}</bdi>
              </>
            )}
          </p>
        </>
      ) : (
        <p className="text-muted-foreground">{t("noPrevious")}</p>
      )}
    </div>
  );
}

/**
 * The verification checklist (brief 4.1.4): call the number already on file, never one
 * from the request, read the new details back, record what the vendor said. Only the
 * on-file number is ever shown here; the API never sends any other.
 */
export function VerificationChecklist({
  ticket,
  onChange,
  onStepUp,
}: {
  ticket: PayTicketDetail;
  onChange: (t: PayTicketDetail) => void;
  onStepUp: () => void;
}) {
  const t = useTranslations("Pay.verify");
  const ids = useId();
  const [result, setResult] = useState<CallResult | null>(null);
  const [notes, setNotes] = useState("");
  const [attested, setAttested] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const v = ticket.verification;
  const phone = v.phone_on_file;

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!result) return;
    setBusy(true);
    setError(null);
    const response = await recordCall(ticket.id, { result, notes, called_number_on_file: attested });
    setBusy(false);
    if (!response.ok) {
      if (response.code === "second_factor_required") onStepUp();
      setError(response.code);
      return;
    }
    setResult(null);
    setNotes("");
    setAttested(false);
    onChange(response.ticket);
  }

  return (
    <Panel title={t("title")} data-verification-checklist>
      <ol className="list-decimal space-y-1 ps-5 text-sm">
        <li>{t("step1")}</li>
        <li>{t("step2")}</li>
        <li>{t("step3")}</li>
        <li>{t("step4")}</li>
      </ol>
      <div className="flex flex-wrap items-center gap-3 rounded-lg border bg-background p-3" data-on-file-number>
        <Phone aria-hidden className="size-5 text-primary" />
        <div>
          <p className="eyebrow">{t("numberOnFile")}</p>
          {phone ? (
            <a href={`tel:${phone}`} className="font-mono text-lg font-medium" dir="ltr" data-phone-on-file>
              <bdi>{phone}</bdi>
            </a>
          ) : (
            <p className="text-sm" data-phone-hidden>
              {t("numberHidden", { last4: v.phone_last4 })}
            </p>
          )}
        </div>
      </div>
      <p className="text-sm font-medium" data-never-request-number>
        {t("neverRequestNumber")}
      </p>
      {ticket.request_had_contact_details && (
        <p className="text-sm text-muted-foreground" data-request-had-contact>
          {t("requestHadContact")}
        </p>
      )}
      {v.phone_recently_changed && (
        <p className="flex items-start gap-2 rounded-lg border border-destructive/40 p-3 text-sm" role="note" data-phone-recently-changed>
          <AlertTriangle aria-hidden className="mt-0.5 size-4 shrink-0 text-destructive" />
          {t("phoneRecentlyChanged")}
        </p>
      )}
      <details className="rounded-lg border p-3 text-sm" open data-call-script>
        <summary className="cursor-pointer font-medium">{t("script.title")}</summary>
        <ol className="mt-2 list-decimal space-y-1 ps-5">
          <li>{t("script.line1", { vendor: ticket.vendor_name })}</li>
          <li>{t("script.line2")}</li>
          <li>
            {t("script.line3")}{" "}
            {ticket.proposed_iban && (
              <bdi dir="ltr" className="font-mono">
                {groupIban(ticket.proposed_iban)}
              </bdi>
            )}
          </li>
          <li>{t("script.line4", { name: ticket.proposed_account.account_name })}</li>
          <li>{t("script.line5")}</li>
        </ol>
      </details>
      {ticket.can_verify ? (
        <form onSubmit={submit} className="space-y-3" data-call-form>
          <fieldset className="space-y-2">
            <legend className="text-sm font-medium">{t("result")}</legend>
            <div role="radiogroup" aria-label={t("result")} className="grid gap-2 sm:grid-cols-3">
              {(["confirmed", "not_confirmed", "no_answer"] as const).map((r) => (
                <button
                  key={r}
                  type="button"
                  role="radio"
                  aria-checked={result === r}
                  data-result={r}
                  onClick={() => setResult(r)}
                  className={cn(
                    "min-h-11 rounded-lg border px-3 text-sm font-medium transition-colors focus-visible:ring-[3px] focus-visible:ring-ring/25 focus-visible:outline-none",
                    result === r ? "border-primary bg-primary/10" : "bg-surface hover:bg-accent",
                  )}
                >
                  {t(`results.${r}`)}
                </button>
              ))}
            </div>
          </fieldset>
          <div className="space-y-1.5">
            <Label htmlFor={`${ids}-notes`}>{t("notes")}</Label>
            <Textarea id={`${ids}-notes`} maxLength={2000} value={notes} onChange={(e) => setNotes(e.target.value)} />
          </div>
          <label className="flex items-start gap-2 text-sm">
            <Checkbox checked={attested} onChange={(e) => setAttested(e.target.checked)} data-attest />
            {t("attest")}
          </label>
          <ErrorText code={error} />
          <Button type="submit" disabled={busy || !result || !attested} data-record-call>
            {busy && <Loader2 aria-hidden className="animate-spin" />}
            {t("record")}
          </Button>
        </form>
      ) : (
        <p className="text-sm text-muted-foreground" data-cannot-verify>
          {phone ? t("notYou") : t("someoneElse")}
        </p>
      )}
    </Panel>
  );
}

function CancelForm({ ticket, onChange, onStepUp }: { ticket: PayTicketDetail; onChange: (t: PayTicketDetail) => void; onStepUp: () => void }) {
  const t = useTranslations("Pay.cancel");
  const ids = useId();
  const [reason, setReason] = useState("");
  const [keepHold, setKeepHold] = useState(ticket.status === "cooling_off");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    const result = await cancelBankChange(ticket.id, reason, keepHold);
    setBusy(false);
    if (!result.ok) {
      if (result.code === "second_factor_required") onStepUp();
      setError(result.code);
      return;
    }
    onChange(result.ticket);
  }

  return (
    <form onSubmit={submit} className="space-y-3" data-cancel-form>
      <div className="space-y-1.5">
        <Label htmlFor={`${ids}-reason`}>{t("reason")}</Label>
        <Textarea id={`${ids}-reason`} required maxLength={2000} value={reason} onChange={(e) => setReason(e.target.value)} />
      </div>
      <label className="flex items-start gap-2 text-sm">
        <Checkbox checked={keepHold} onChange={(e) => setKeepHold(e.target.checked)} data-keep-hold />
        {t("keepHold")}
      </label>
      <ErrorText code={error} />
      <Button type="submit" variant="outline" disabled={busy || !reason.trim()}>
        {busy && <Loader2 aria-hidden className="animate-spin" />}
        {t("submit")}
      </Button>
    </form>
  );
}

/** The change ticket page: the comparison, the checklist, the approvals and the hold. */
export function BankChangeView({
  initial,
  totpEnabled,
  returnPath,
}: {
  initial: PayTicketDetail;
  totpEnabled: boolean;
  returnPath: string;
}) {
  const t = useTranslations("Pay.change");
  const format = useFormatter();
  const [ticket, setTicket] = useState(initial);
  const [stepUp, setStepUp] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const when = (iso: string | null | undefined) => (iso ? format.dateTime(new Date(iso), { dateStyle: "medium", timeStyle: "short" }) : "");

  async function retryApproval() {
    setBusy(true);
    setError(null);
    const result = await startApproval(ticket.id);
    setBusy(false);
    if (result.ok) setTicket(result.ticket);
    else {
      if (result.code === "second_factor_required") setStepUp(true);
      setError(result.code);
    }
  }

  return (
    <div className="space-y-6" data-bank-change={ticket.reference}>
      <div className="flex flex-wrap items-center gap-2">
        <TicketStatusBadge status={ticket.status} />
        {ticket.vendor_payment_hold ? (
          <HoldBadge reason={ticket.vendor_hold_reason} />
        ) : (
          <span className="text-sm text-muted-foreground" data-no-hold>
            {t("noHold")}
          </span>
        )}
        <Link href={`/pay/vendors/${ticket.vendor_id}`} className="text-sm underline">
          <bdi>{ticket.vendor_name}</bdi> (<bdi>{ticket.vendor_reference}</bdi>)
        </Link>
      </div>
      <ChangeProgress status={ticket.status} />
      {stepUp && <StepUpPrompt totpEnabled={totpEnabled} returnPath={returnPath} compact />}
      {ticket.outcome && ticket.status !== "completed" && (
        <p className="rounded-lg border p-3 text-sm" data-outcome={ticket.outcome}>
          {t(`outcome.${ticket.outcome.split(":")[0]}` as "outcome.rejected_by_approver")}
          {ticket.hold_kept ? ` ${t("holdKept")}` : ""}
        </p>
      )}

      <Panel title={t("compare")} data-compare>
        <div className="flex flex-col items-stretch gap-3 sm:flex-row sm:items-center">
          <AccountCard title={t("current")} account={ticket.previous_account} />
          <ArrowRight aria-hidden className="size-5 shrink-0 self-center text-muted-foreground rtl:rotate-180" />
          <AccountCard title={t("proposed")} account={ticket.proposed_account} full={ticket.proposed_iban} />
        </div>
        <p className="text-xs text-muted-foreground">
          {t("requested", { when: when(ticket.created_at), source: t(`source.${ticket.source}` as "source.email") })}
        </p>
        {ticket.note && <p className="text-sm">{ticket.note}</p>}
      </Panel>

      {ticket.status === "awaiting_verification" && (
        <VerificationChecklist ticket={ticket} onChange={setTicket} onStepUp={() => setStepUp(true)} />
      )}

      {ticket.verification.calls.length > 0 && (
        <Panel title={t("calls")} data-calls>
          <ul className="space-y-2 text-sm">
            {ticket.verification.calls.map((c) => (
              <li key={c.id} className="rounded-lg border p-3" data-call={c.result}>
                <p className="font-medium">
                  {t(`callResult.${c.result}` as "callResult.confirmed")} · {when(c.created_at)}
                </p>
                <p className="text-xs text-muted-foreground">{t("calledEnding", { last4: c.number_last4 })}</p>
                {c.notes && <p className="mt-1">{c.notes}</p>}
              </li>
            ))}
          </ul>
        </Panel>
      )}

      <Panel title={t("approvals")} data-approvals>
        <p className="flex items-start gap-2 text-sm text-muted-foreground">
          <ShieldCheck aria-hidden className="mt-0.5 size-4 shrink-0" />
          {t("sod")}
        </p>
        {ticket.approval_steps.length > 0 ? (
          <ul className="space-y-1 text-sm" data-approval-steps>
            {ticket.approval_steps.map((s) => (
              <li key={s.name} data-approval-step={s.name} data-step-status={s.status}>
                {t(`approvalStep.${s.name}` as "approvalStep.first_approval")}: {t(`stepStatus.${s.status}` as "stepStatus.active")}
              </li>
            ))}
          </ul>
        ) : (
          <p className="text-sm">{ticket.status === "awaiting_verification" ? t("afterCall") : t("noApproval")}</p>
        )}
        {ticket.approval_id && ticket.status === "awaiting_approval" && (
          <Link href={`/inbox/${ticket.approval_id}`} className="inline-flex h-8 items-center rounded-md border px-3 text-[0.8rem] hover:bg-accent" data-open-inbox>
            {t("openInbox")}
          </Link>
        )}
        {ticket.can_start_approval && (
          <Button size="sm" variant="outline" disabled={busy} onClick={() => void retryApproval()} data-start-approval>
            {busy && <Loader2 aria-hidden className="animate-spin" />}
            {t("startApproval")}
          </Button>
        )}
        {ticket.status === "cooling_off" && ticket.activates_at && (
          <p className="text-sm font-medium" data-cooling-off>
            {t("coolingOff", { when: when(ticket.activates_at), hours: ticket.cooling_off_hours })}
          </p>
        )}
        {ticket.status === "completed" && ticket.completed_at && <p className="text-sm">{t("activated", { when: when(ticket.completed_at) })}</p>}
        <ErrorText code={error} />
      </Panel>

      {ticket.can_cancel && isOpenTicket(ticket.status) && (
        <Panel title={t("cancelTitle")}>
          <CancelForm ticket={ticket} onChange={setTicket} onStepUp={() => setStepUp(true)} />
        </Panel>
      )}
    </div>
  );
}

/** Every change ticket, open ones first. */
export function BankChangeList({ items }: { items: PayTicketSummary[] }) {
  const t = useTranslations("Pay.changes");
  const format = useFormatter();
  const [openOnly, setOpenOnly] = useState(true);
  const shown = items.filter((i) => !openOnly || isOpenTicket(i.status));
  return (
    <div className="space-y-3">
      <label className="flex items-center gap-2 text-sm">
        <Checkbox checked={openOnly} onChange={(e) => setOpenOnly(e.target.checked)} data-open-only />
        {t("openOnly")}
      </label>
      {shown.length === 0 ? (
        <p className="text-sm text-muted-foreground" data-no-changes>
          {t("empty")}
        </p>
      ) : (
        <ul className="divide-y rounded-xl border bg-surface" data-change-list>
          {shown.map((i) => (
            <li key={i.id} className="flex flex-wrap items-center gap-2 px-4 py-3 text-sm" data-change={i.reference}>
              <Link href={`/pay/bank-changes/${i.id}`} className="font-medium hover:underline">
                <bdi>{i.reference}</bdi>
              </Link>
              <TicketStatusBadge status={i.status} />
              <span>
                <bdi>{i.vendor_name}</bdi>
              </span>
              <bdi dir="ltr" className="text-muted-foreground">
                {i.new_iban_masked}
              </bdi>
              <span className="ms-auto text-xs text-muted-foreground">
                {format.dateTime(new Date(i.created_at), { dateStyle: "medium" })}
              </span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
