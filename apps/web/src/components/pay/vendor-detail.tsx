// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
"use client";

import { Landmark, Loader2, Phone } from "lucide-react";
import { useFormatter, useTranslations } from "next-intl";
import { useId, useState } from "react";
import type * as React from "react";

import { StepUpPrompt } from "@/components/states/step-up-prompt";
import { Button } from "@/components/ui/button";
import { Select, Textarea } from "@/components/ui/form-controls";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Link, useRouter } from "@/i18n/navigation";
import { requestBankChange, vendorAction } from "@/lib/actions/pay";
import type { PayVendorDetail } from "@/lib/pay/types";

import { ErrorText, HoldBadge, Panel, TicketStatusBadge, VendorStatusBadge } from "./common";

const SOURCES = ["email", "staff", "invoice", "vendor_portal"] as const;

/** Ask for new bank details: opens a locked ticket and holds payments at once. */
export function BankChangeRequestForm({ vendor }: { vendor: PayVendorDetail }) {
  const t = useTranslations("Pay.request");
  const router = useRouter();
  const ids = useId();
  const [bank, setBank] = useState({ iban: "", account_name: "", bank_name: "", swift: "" });
  const [source, setSource] = useState<(typeof SOURCES)[number]>("email");
  const [note, setNote] = useState("");
  const [contact, setContact] = useState({ name: "", phone: "", email: "" });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    const hasContact = contact.name || contact.phone || contact.email;
    const result = await requestBankChange(vendor.id, {
      bank,
      source,
      note,
      ...(hasContact ? { request_contact: contact } : {}),
    });
    setBusy(false);
    if (!result.ok) {
      setError(result.code);
      return;
    }
    router.push(`/pay/bank-changes/${result.ticket.id}`);
  }

  const id = (name: string) => `${ids}-${name}`;
  return (
    <form onSubmit={submit} className="space-y-4" data-bank-change-form>
      <p className="text-sm text-muted-foreground">{t("intro")}</p>
      <div className="grid gap-4 sm:grid-cols-2">
        {(["iban", "account_name", "bank_name", "swift"] as const).map((name) => (
          <div key={name} className="space-y-1.5">
            <Label htmlFor={id(name)}>{t(`field.${name}`)}</Label>
            <Input
              id={id(name)}
              required={name === "iban" || name === "account_name"}
              maxLength={name === "iban" ? 64 : name === "swift" ? 16 : 200}
              dir={name === "iban" || name === "swift" ? "ltr" : undefined}
              value={bank[name]}
              onChange={(e) => setBank((b) => ({ ...b, [name]: e.target.value }))}
            />
          </div>
        ))}
        <div className="space-y-1.5">
          <Label htmlFor={id("source")}>{t("field.source")}</Label>
          <Select id={id("source")} value={source} onChange={(e) => setSource(e.target.value as (typeof SOURCES)[number])}>
            {SOURCES.map((s) => (
              <option key={s} value={s}>
                {t(`source.${s}`)}
              </option>
            ))}
          </Select>
        </div>
      </div>
      <div className="space-y-1.5">
        <Label htmlFor={id("note")}>{t("field.note")}</Label>
        <Textarea id={id("note")} maxLength={2000} value={note} onChange={(e) => setNote(e.target.value)} />
      </div>
      <fieldset className="space-y-2 rounded-lg border border-dashed p-3">
        <legend className="px-1 text-sm font-medium">{t("contactTitle")}</legend>
        <p className="text-xs text-muted-foreground" data-request-contact-hint>
          {t("contactHint")}
        </p>
        <div className="grid gap-3 sm:grid-cols-3">
          {(["name", "phone", "email"] as const).map((name) => (
            <div key={name} className="space-y-1.5">
              <Label htmlFor={id(`contact-${name}`)}>{t(`contact.${name}`)}</Label>
              <Input
                id={id(`contact-${name}`)}
                maxLength={name === "email" ? 254 : name === "phone" ? 40 : 120}
                dir={name === "name" ? undefined : "ltr"}
                value={contact[name]}
                onChange={(e) => setContact((c) => ({ ...c, [name]: e.target.value }))}
              />
            </div>
          ))}
        </div>
      </fieldset>
      <ErrorText code={error} />
      <Button type="submit" disabled={busy} data-bank-change-submit>
        {busy && <Loader2 aria-hidden className="animate-spin" />}
        {t("submit")}
      </Button>
    </form>
  );
}

/** A short reason and a button: accept, reject, hold or release. */
function ReasonAction({
  label,
  action,
  vendorId,
  optional = false,
  onDone,
  onStepUp,
  variant = "outline",
}: {
  label: string;
  action: "accept" | "reject" | "hold" | "release";
  vendorId: string;
  optional?: boolean;
  onDone: (v: PayVendorDetail) => void;
  onStepUp: () => void;
  variant?: "outline" | "default";
}) {
  const t = useTranslations("Pay.vendor");
  const ids = useId();
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function run(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    const result = await vendorAction(vendorId, action, text);
    setBusy(false);
    if (result.ok) {
      setText("");
      onDone(result.vendor);
      return;
    }
    if (result.code === "second_factor_required") onStepUp();
    setError(result.code);
  }

  return (
    <form onSubmit={run} className="space-y-2" data-vendor-action={action}>
      <Label htmlFor={`${ids}-r`}>{optional ? t("noteOptional") : t("reason")}</Label>
      <Textarea id={`${ids}-r`} required={!optional} maxLength={2000} value={text} onChange={(e) => setText(e.target.value)} />
      <ErrorText code={error} />
      <Button type="submit" size="sm" variant={variant} disabled={busy}>
        {busy && <Loader2 aria-hidden className="animate-spin" />}
        {label}
      </Button>
    </form>
  );
}

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="grid grid-cols-[10rem_1fr] gap-2 text-sm">
      <dt className="text-muted-foreground">{label}</dt>
      <dd className="min-w-0">{children}</dd>
    </div>
  );
}

/** One vendor: record, bank details (masked), risk, hold, change tickets, review. */
export function VendorDetailView({
  initial,
  totpEnabled,
  returnPath,
}: {
  initial: PayVendorDetail;
  totpEnabled: boolean;
  returnPath: string;
}) {
  const t = useTranslations("Pay.vendor");
  const tr = useTranslations("Pay.risk");
  const format = useFormatter();
  const [vendor, setVendor] = useState(initial);
  const [stepUp, setStepUp] = useState(false);
  const [requesting, setRequesting] = useState(false);
  const date = (iso: string | null | undefined) => (iso ? format.dateTime(new Date(iso), { dateStyle: "medium" }) : "");
  const address = Object.values(vendor.address as Record<string, string>).filter(Boolean).join(", ");

  return (
    <div className="space-y-6" data-vendor-detail={vendor.reference}>
      <div className="flex flex-wrap items-center gap-2">
        <VendorStatusBadge status={vendor.status} />
        {vendor.payment_hold && <HoldBadge reason={vendor.hold_reason} />}
        <span className="text-sm text-muted-foreground">
          <bdi>{vendor.reference}</bdi>
        </span>
      </div>
      {stepUp && <StepUpPrompt totpEnabled={totpEnabled} returnPath={returnPath} compact />}

      {vendor.status === "pending" && vendor.can_write && (
        <Panel title={t("review.title")} data-onboarding-review>
          <p className="text-sm text-muted-foreground">{t("review.intro")}</p>
          {vendor.review_reasons.length > 0 && (
            <ul className="list-disc ps-5 text-sm" data-review-reasons>
              {vendor.review_reasons.map((r) => (
                <li key={r}>
                  <bdi>{r}</bdi>
                </li>
              ))}
            </ul>
          )}
          {vendor.duplicates.length > 0 && (
            <div className="space-y-1 text-sm" data-duplicates>
              <p className="font-medium">{t("review.duplicates")}</p>
              <ul className="list-disc ps-5">
                {vendor.duplicates.map((d) => (
                  <li key={d.vendor_id}>
                    <Link href={`/pay/vendors/${d.vendor_id}`} className="underline">
                      <bdi>{d.legal_name}</bdi>
                    </Link>{" "}
                    (<bdi>{d.reference}</bdi>)
                  </li>
                ))}
              </ul>
            </div>
          )}
          {!vendor.open_ticket_id && (
            <div className="grid gap-4 sm:grid-cols-2">
              <ReasonAction label={t("review.accept")} action="accept" optional vendorId={vendor.id} onDone={setVendor} onStepUp={() => setStepUp(true)} variant="default" />
              <ReasonAction label={t("review.reject")} action="reject" vendorId={vendor.id} onDone={setVendor} onStepUp={() => setStepUp(true)} />
            </div>
          )}
        </Panel>
      )}

      <div className="grid gap-6 lg:grid-cols-2">
        <Panel title={t("record")}>
          <dl className="space-y-2">
            {vendor.legal_name_ar && (
              <Row label={t("field.legal_name_ar")}>
                <bdi lang="ar">{vendor.legal_name_ar}</bdi>
              </Row>
            )}
            {vendor.trade_name && <Row label={t("field.trade_name")}>{vendor.trade_name}</Row>}
            <Row label={t("field.country")}>{vendor.country}</Row>
            {vendor.trade_licence && (
              <Row label={t("field.trade_licence")}>
                <bdi>{vendor.trade_licence}</bdi>
              </Row>
            )}
            <Row label={t("field.trn")}>
              {vendor.trn ? (
                <>
                  <bdi dir="ltr">{vendor.trn}</bdi> <span className="text-xs text-muted-foreground">({t(`trnStatus.${vendor.trn_status}` as "trnStatus.not_verified")})</span>
                </>
              ) : (
                t("none")
              )}
            </Row>
            {address && <Row label={t("field.address")}>{address}</Row>}
            <Row label={t("field.phone_on_file")}>
              {vendor.phone_on_file ? (
                <span className="inline-flex items-center gap-1">
                  <Phone aria-hidden className="size-3.5" />
                  <bdi dir="ltr">{vendor.phone_on_file}</bdi>
                </span>
              ) : (
                t("none")
              )}
            </Row>
            {vendor.email_domains.length > 0 && (
              <Row label={t("field.email_domains")}>
                <bdi dir="ltr">{vendor.email_domains.join(", ")}</bdi>
              </Row>
            )}
            {vendor.contacts.map((c) => (
              <Row key={`${c.name}-${c.email ?? ""}`} label={t("field.contact")}>
                {c.name}
                {c.role ? `, ${c.role}` : ""}
                {c.email && (
                  <>
                    {" "}
                    · <bdi dir="ltr">{c.email}</bdi>
                  </>
                )}
                {c.phone && (
                  <>
                    {" "}
                    · <bdi dir="ltr">{c.phone}</bdi>
                  </>
                )}
              </Row>
            ))}
          </dl>
        </Panel>

        <Panel title={tr("title")} data-risk-profile>
          <dl className="space-y-2">
            <Row label={tr("age")}>{tr("days", { count: Number(vendor.risk.record_age_days ?? 0) })}</Row>
            <Row label={tr("requests")}>{String(vendor.risk.bank_change_requests_90d ?? 0)}</Row>
            <Row label={tr("payments")}>{tr("paymentsLater")}</Row>
          </dl>
          {Array.isArray(vendor.risk.signals) && vendor.risk.signals.length > 0 ? (
            <ul className="list-disc ps-5 text-sm" data-signals>
              {(vendor.risk.signals as string[]).map((s) => (
                <li key={s} data-signal={s}>
                  {tr(`signal.${s}` as "signal.new_vendor")}
                </li>
              ))}
            </ul>
          ) : (
            <p className="text-sm text-muted-foreground">{tr("noSignals")}</p>
          )}
        </Panel>
      </div>

      <Panel title={t("bankTitle")} data-bank-accounts>
        <p className="text-sm text-muted-foreground">{t("bankIntro")}</p>
        {vendor.accounts.length === 0 ? (
          <p className="text-sm">{t("noAccounts")}</p>
        ) : (
          <ul className="divide-y rounded-lg border">
            {vendor.accounts.map((a) => (
              <li key={a.id} className="flex flex-wrap items-center gap-2 px-3 py-2 text-sm" data-account={a.status}>
                <Landmark aria-hidden className="size-4 text-muted-foreground" />
                <bdi dir="ltr" className="font-mono">
                  {a.iban_masked}
                </bdi>
                <span>{a.account_name}</span>
                {a.bank_name && <span className="text-muted-foreground">{a.bank_name}</span>}
                <span className="ms-auto text-xs text-muted-foreground">{t(`accountStatus.${a.status}` as "accountStatus.active")}</span>
              </li>
            ))}
          </ul>
        )}
        {vendor.can_request_change && !requesting && (
          <Button variant="outline" onClick={() => setRequesting(true)} data-request-change>
            {t("requestChange")}
          </Button>
        )}
        {requesting && <BankChangeRequestForm vendor={vendor} />}
      </Panel>

      {vendor.tickets.length > 0 && (
        <Panel title={t("changesTitle")} data-vendor-tickets>
          <ul className="divide-y rounded-lg border">
            {vendor.tickets.map((ticket) => (
              <li key={ticket.id} className="flex flex-wrap items-center gap-2 px-3 py-2 text-sm">
                <Link href={`/pay/bank-changes/${ticket.id}`} className="font-medium hover:underline">
                  <bdi>{ticket.reference}</bdi>
                </Link>
                <TicketStatusBadge status={ticket.status} />
                <bdi dir="ltr" className="text-muted-foreground">
                  {ticket.new_iban_masked}
                </bdi>
                <span className="ms-auto text-xs text-muted-foreground">{date(ticket.created_at)}</span>
              </li>
            ))}
          </ul>
        </Panel>
      )}

      {vendor.can_manage_hold && (
        <Panel title={t("holdTitle")} data-hold-panel>
          <p className="text-sm text-muted-foreground">{vendor.payment_hold ? t("holdOn") : t("holdOff")}</p>
          {vendor.payment_hold ? (
            <ReasonAction label={t("release")} action="release" vendorId={vendor.id} onDone={setVendor} onStepUp={() => setStepUp(true)} />
          ) : (
            <ReasonAction label={t("placeHold")} action="hold" vendorId={vendor.id} onDone={setVendor} onStepUp={() => setStepUp(true)} />
          )}
        </Panel>
      )}
    </div>
  );
}
