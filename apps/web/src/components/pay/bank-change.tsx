// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
"use client";

import {
  AlertTriangle,
  ArrowRight,
  Check,
  CheckCircle2,
  Clock,
  Hourglass,
  Loader2,
  Lock,
  Phone,
  PhoneOff,
  ShieldCheck,
  X,
} from "lucide-react";
import { useFormatter, useTranslations } from "next-intl";
import { useId, useState } from "react";
import type * as React from "react";

import { StepUpPrompt } from "@/components/states/step-up-prompt";
import { Button } from "@/components/ui/button";
import { Checkbox, Textarea } from "@/components/ui/form-controls";
import { Label } from "@/components/ui/label";
import { Link } from "@/i18n/navigation";
import { cancelBankChange, recordCall, recordCallbackNumber, startApproval } from "@/lib/actions/pay";
import { CHANGE_STEPS, changeStepIndex, groupIban, isOpenTicket } from "@/lib/pay/format";
import type { PayBankAccount, PayTicketDetail, PayTicketSummary } from "@/lib/pay/types";
import { cn } from "@/lib/utils";

import { CallbackSourceFields, CallbackSourceView, type CallbackValue, EMPTY_CALLBACK, callbackComplete } from "./callback-source";
import { ErrorText, HoldBadge, Panel, TicketStatusBadge } from "./common";
import { Callout } from "./ledger";

type CallResult = "confirmed" | "not_confirmed" | "no_answer";

/** Where the change stands: requested, call back, two approvals, cooling-off, active. */
export function ChangeProgress({ status }: { status: string }) {
  const t = useTranslations("Pay.change.steps");
  const at = changeStepIndex(status);
  return (
    <ol className="grid grid-cols-2 gap-x-2 gap-y-3 text-xs sm:grid-cols-5" data-change-progress={status}>
      {CHANGE_STEPS.map((step, i) => {
        const done = at > i || status === "completed";
        const current = at === i && status !== "completed";
        // The call back is the lock: while it is the current step it reads as danger.
        // Approvals and the cooling-off read as warning. Never saffron.
        const tone = current ? (step === "call" ? "danger" : "warning") : done ? "success" : "todo";
        return (
          <li
            key={step}
            aria-current={current ? "step" : undefined}
            data-step={step}
            data-state={done ? "done" : current ? "current" : "todo"}
            className={cn(
              "flex flex-col gap-1.5 border-t-4 pt-2",
              tone === "success" && "border-success text-success-ink",
              tone === "danger" && "border-destructive font-medium text-danger-ink",
              tone === "warning" && "border-warning-ink font-medium text-warning-ink",
              tone === "todo" && "border-rule text-muted-foreground",
            )}
          >
            <span className="inline-flex items-center gap-1">
              {done ? (
                <Check aria-hidden className="size-3.5" />
              ) : current && step === "call" ? (
                <Lock aria-hidden className="size-3.5" />
              ) : current ? (
                <Hourglass aria-hidden className="size-3.5" />
              ) : (
                <span aria-hidden className="font-mono">
                  {i + 1}
                </span>
              )}
              {t(step)}
            </span>
          </li>
        );
      })}
    </ol>
  );
}

/**
 * The lock, stated before anything else: while a change is open nothing is paid to the
 * new account. Danger until the call back confirms, warning while two people approve and
 * the cooling-off runs, success once the new details are active. Icon and words carry
 * the state as well as the colour.
 */
export function ChangeLock({ status, held }: { status: string; held: boolean }) {
  const t = useTranslations("Pay.change.lock");
  if (status === "awaiting_verification") {
    return (
      <Callout tone="danger" icon={<Lock aria-hidden />} title={t("lockedTitle")} data-change-lock="locked">
        <p className="text-danger-ink">{t("lockedBody")}</p>
      </Callout>
    );
  }
  if (status === "awaiting_approval") {
    return (
      <Callout tone="warning" icon={<ShieldCheck aria-hidden />} title={t("approvalsTitle")} data-change-lock="approvals">
        <p className="text-warning-ink">{t("approvalsBody")}</p>
      </Callout>
    );
  }
  if (status === "cooling_off") {
    return (
      <Callout tone="warning" icon={<Hourglass aria-hidden />} title={t("coolingTitle")} data-change-lock="cooling">
        <p className="text-warning-ink">{t("coolingBody")}</p>
      </Callout>
    );
  }
  if (status === "completed") {
    return (
      <Callout tone="success" icon={<CheckCircle2 aria-hidden />} title={t("activeTitle")} data-change-lock="active">
        {held && <p className="text-success-ink">{t("activeHeld")}</p>}
      </Callout>
    );
  }
  return null;
}

function AccountCard({
  title,
  account,
  full,
  proposed,
}: {
  title: string;
  account: PayBankAccount | null | undefined;
  full?: string | null;
  proposed?: boolean;
}) {
  const t = useTranslations("Pay.change");
  return (
    <div
      className={cn(
        "min-w-0 flex-1 space-y-1.5 rounded-lg border p-4 text-sm",
        proposed ? "border-warning-ink/40 bg-warning-soft/50" : "border-border bg-surface-sunken",
      )}
    >
      <p className="eyebrow">{title}</p>
      {account ? (
        <>
          <p className="font-mono text-base font-medium break-all" dir="ltr">
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
    <Panel title={t("title")} className="border-destructive/40 border-t-4 border-t-destructive" data-verification-checklist>
      <ol className="list-decimal space-y-1 ps-5 text-sm marker:font-mono marker:text-muted-foreground">
        <li>{t("step1")}</li>
        <li>{t("step2")}</li>
        <li>{t("step3")}</li>
        <li>{t("step4")}</li>
      </ol>
      <div className="flex flex-wrap items-center gap-3 rounded-lg border border-border bg-surface-sunken p-4" data-on-file-number>
        <span aria-hidden className="flex size-10 items-center justify-center rounded-full bg-foreground text-background">
          <Phone className="size-5" />
        </span>
        <div>
          <p className="eyebrow">{t("numberOnFile")}</p>
          {phone ? (
            <a href={`tel:${phone}`} className="font-mono text-xl font-medium tracking-wide underline-offset-4 hover:underline" dir="ltr" data-phone-on-file>
              <bdi>{phone}</bdi>
            </a>
          ) : (
            <p className="text-sm" data-phone-hidden>
              {t("numberHidden", { last4: v.phone_last4 })}
            </p>
          )}
        </div>
      </div>
      <CallbackSourceView ticket={ticket} />
      {v.callback_required && <RecordCallbackForm ticket={ticket} onChange={onChange} />}
      <p className="flex items-start gap-2 text-sm font-medium text-danger-ink" data-never-request-number>
        <PhoneOff aria-hidden className="mt-0.5 size-4 shrink-0" />
        {t("neverRequestNumber")}
      </p>
      {ticket.request_had_contact_details && (
        <p className="text-sm text-muted-foreground" data-request-had-contact>
          {t("requestHadContact")}
        </p>
      )}
      {v.phone_recently_changed && (
        <p className="flex items-start gap-2 rounded-lg border border-destructive/45 bg-danger-soft p-3 text-sm text-danger-ink" role="note" data-phone-recently-changed>
          <AlertTriangle aria-hidden className="mt-0.5 size-4 shrink-0" />
          {t("phoneRecentlyChanged")}
        </p>
      )}
      <details className="rounded-lg border border-border p-4 text-sm" open data-call-script>
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
                    "min-h-11 rounded-lg border px-3 text-sm font-medium transition-colors",
                    result !== r && "border-input bg-surface hover:bg-accent",
                    result === r && r === "confirmed" && "border-success bg-success-soft text-success-ink ring-1 ring-success",
                    result === r && r === "not_confirmed" && "border-destructive bg-danger-soft text-danger-ink ring-1 ring-destructive",
                    result === r && r === "no_answer" && "border-warning-ink bg-warning-soft text-warning-ink ring-1 ring-warning-ink",
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

/** An onboarding change without a number from an independent source cannot be
 * verified: AP records one here first (security review 2026-10, M-05). */
function RecordCallbackForm({ ticket, onChange }: { ticket: PayTicketDetail; onChange: (t: PayTicketDetail) => void }) {
  const t = useTranslations("Pay.callback");
  const [callback, setCallback] = useState<CallbackValue>(EMPTY_CALLBACK);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!callbackComplete(callback)) return;
    setBusy(true);
    setError(null);
    const response = await recordCallbackNumber(ticket.id, callback);
    setBusy(false);
    if (!response.ok) {
      setError(response.code);
      return;
    }
    setCallback(EMPTY_CALLBACK);
    onChange(response.ticket);
  }

  return (
    <form onSubmit={submit} className="space-y-3" data-record-callback>
      <p className="flex items-start gap-2 rounded-lg border border-destructive/45 bg-danger-soft p-3 text-sm text-danger-ink" role="note">
        <AlertTriangle aria-hidden className="mt-0.5 size-4 shrink-0" />
        {t("required")}
      </p>
      <CallbackSourceFields value={callback} onChange={setCallback} />
      <ErrorText code={error} />
      <Button type="submit" size="sm" disabled={busy || !callbackComplete(callback)}>
        {busy && <Loader2 aria-hidden className="animate-spin" />}
        {t("record")}
      </Button>
    </form>
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
      <ChangeLock status={ticket.status} held={Boolean(ticket.vendor_payment_hold)} />
      <div className="flex flex-wrap items-center gap-2">
        <TicketStatusBadge status={ticket.status} />
        {ticket.vendor_payment_hold ? (
          <HoldBadge reason={ticket.vendor_hold_reason} />
        ) : (
          <span className="text-sm text-muted-foreground" data-no-hold>
            {t("noHold")}
          </span>
        )}
        <Link href={`/pay/vendors/${ticket.vendor_id}`} className="link-inline text-sm sm:ms-auto">
          <bdi>{ticket.vendor_name}</bdi> (<bdi>{ticket.vendor_reference}</bdi>)
        </Link>
      </div>
      <ChangeProgress status={ticket.status} />
      {stepUp && <StepUpPrompt totpEnabled={totpEnabled} returnPath={returnPath} compact />}
      {ticket.outcome && ticket.status !== "completed" && (
        <p className="rounded-lg border border-border bg-surface-sunken p-3 text-sm" data-outcome={ticket.outcome}>
          {t(`outcome.${ticket.outcome.split(":")[0]}` as "outcome.rejected_by_approver")}
          {ticket.hold_kept ? ` ${t("holdKept")}` : ""}
        </p>
      )}

      <Panel title={t("compare")} data-compare>
        <div className="flex flex-col items-stretch gap-3 sm:flex-row sm:items-center">
          <AccountCard title={t("current")} account={ticket.previous_account} />
          <ArrowRight aria-hidden className="size-5 shrink-0 self-center text-muted-foreground rtl:rotate-180" />
          <AccountCard
            title={t("proposed")}
            account={ticket.proposed_account}
            full={ticket.proposed_iban}
            proposed={ticket.status !== "completed"}
          />
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
              <li
                key={c.id}
                className={cn(
                  "flex items-start gap-3 rounded-lg border p-3",
                  c.result === "confirmed" && "border-success/35 bg-success-soft",
                  c.result === "not_confirmed" && "border-destructive/45 bg-danger-soft",
                  c.result === "no_answer" && "border-warning-ink/35 bg-warning-soft",
                )}
                data-call={c.result}
              >
                {c.result === "confirmed" ? (
                  <CheckCircle2 aria-hidden className="mt-0.5 size-5 shrink-0 text-success-ink" />
                ) : c.result === "not_confirmed" ? (
                  <X aria-hidden className="mt-0.5 size-5 shrink-0 text-danger-ink" />
                ) : (
                  <PhoneOff aria-hidden className="mt-0.5 size-5 shrink-0 text-warning-ink" />
                )}
                <div className="min-w-0 flex-1">
                  <p
                    className={cn(
                      "font-medium",
                      c.result === "confirmed" && "text-success-ink",
                      c.result === "not_confirmed" && "text-danger-ink",
                      c.result === "no_answer" && "text-warning-ink",
                    )}
                  >
                    {t(`callResult.${c.result}` as "callResult.confirmed")} ·{" "}
                    <span className="font-mono text-xs">{when(c.created_at)}</span>
                  </p>
                  <p className="font-mono text-xs text-muted-foreground">{t("calledEnding", { last4: c.number_last4 })}</p>
                  {c.notes && <p className="mt-1">{c.notes}</p>}
                </div>
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
          <ul className="grid gap-3 text-sm sm:grid-cols-2" data-approval-steps>
            {ticket.approval_steps.map((s, i) => (
              <li
                key={s.name}
                data-approval-step={s.name}
                data-step-status={s.status}
                className={cn(
                  "flex items-center gap-3 rounded-lg border p-3",
                  s.status === "approved" && "border-success/35 bg-success-soft text-success-ink",
                  s.status === "rejected" && "border-destructive/45 bg-danger-soft text-danger-ink",
                  (s.status === "active" || s.status === "changes_requested") &&
                    "border-warning-ink/35 bg-warning-soft text-warning-ink",
                  (s.status === "waiting" || s.status === "cancelled") && "border-dashed border-input text-muted-foreground",
                )}
              >
                <span
                  aria-hidden
                  className="flex size-8 shrink-0 items-center justify-center rounded-full border-2 border-current font-mono text-xs font-semibold"
                >
                  {s.status === "approved" ? <Check className="size-4" /> : s.status === "rejected" ? <X className="size-4" /> : i + 1}
                </span>
                <span className="font-medium">
                  {t(`approvalStep.${s.name}` as "approvalStep.first_approval")}: {t(`stepStatus.${s.status}` as "stepStatus.active")}
                </span>
              </li>
            ))}
          </ul>
        ) : (
          <p className="text-sm">{ticket.status === "awaiting_verification" ? t("afterCall") : t("noApproval")}</p>
        )}
        {ticket.approval_id && ticket.status === "awaiting_approval" && (
          <Link
            href={`/inbox/${ticket.approval_id}`}
            className="inline-flex h-8 items-center rounded-md border border-input bg-surface px-3 text-[0.8rem] hover:bg-accent"
            data-open-inbox
          >
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
          <p
            className="flex items-start gap-2 rounded-lg border border-warning-ink/35 bg-warning-soft p-3 text-sm font-medium text-warning-ink"
            data-cooling-off
          >
            <Clock aria-hidden className="mt-0.5 size-4 shrink-0" />
            <span>{t("coolingOff", { when: when(ticket.activates_at), hours: ticket.cooling_off_hours })}</span>
          </p>
        )}
        {ticket.status === "completed" && ticket.completed_at && (
          <p className="flex items-start gap-2 text-sm text-success-ink">
            <CheckCircle2 aria-hidden className="mt-0.5 size-4 shrink-0" />
            {t("activated", { when: when(ticket.completed_at) })}
          </p>
        )}
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
        <ul className="card-hairline divide-y divide-border" data-change-list>
          {shown.map((i) => (
            <li
              key={i.id}
              className={cn(
                "flex flex-wrap items-center gap-x-3 gap-y-1 border-s-[3px] px-4 py-3 text-sm",
                i.status === "awaiting_verification"
                  ? "border-s-destructive"
                  : i.status === "awaiting_approval" || i.status === "cooling_off"
                    ? "border-s-warning-ink"
                    : "border-s-transparent",
              )}
              data-change={i.reference}
            >
              <Link href={`/pay/bank-changes/${i.id}`} className="font-mono font-medium hover:underline">
                <bdi>{i.reference}</bdi>
              </Link>
              <TicketStatusBadge status={i.status} />
              <span>
                <bdi>{i.vendor_name}</bdi>
              </span>
              <bdi dir="ltr" className="font-mono text-muted-foreground">
                {i.new_iban_masked}
              </bdi>
              <span className="ms-auto font-mono text-xs text-muted-foreground">
                {format.dateTime(new Date(i.created_at), { dateStyle: "medium" })}
              </span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
