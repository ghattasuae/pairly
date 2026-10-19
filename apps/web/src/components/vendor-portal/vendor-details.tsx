// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
"use client";

import { ArrowLeft, Landmark, Loader2, ShieldAlert, ShieldCheck, Users } from "lucide-react";
import { useFormatter, useTranslations } from "next-intl";
import type * as React from "react";
import { useId, useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/form-controls";
import { Input } from "@/components/ui/input";
import { Link } from "@/i18n/navigation";
import { requestBankDetailChange } from "@/lib/actions/vendor-portal";
import type { VendorPortalDetails } from "@/lib/vendor-portal/types";

const KNOWN_ERRORS = new Set(["not_signed_in", "rate_limited", "conflict", "invalid_input"]);

export interface VendorDetailsActions {
  requestChange: typeof requestBankDetailChange;
}

const DEFAULT_ACTIONS: VendorDetailsActions = { requestChange: requestBankDetailChange };

/**
 * The vendor's verified details (brief 5.1 item 11) as accounts payable keeps them, with
 * the bank account masked, the open change requests, the contacts (read only: accounts
 * payable writes to those addresses, so only it changes them; security review 2026-10,
 * L-10), and a bank detail change, which is only ever a request: payments are held
 * until accounts payable calls the number on file and two people approve.
 */
export function VendorDetails({
  details: initial,
  actions = DEFAULT_ACTIONS,
}: {
  details: VendorPortalDetails;
  actions?: VendorDetailsActions;
}) {
  const t = useTranslations("VendorPortal");
  const format = useFormatter();
  const ids = useId();
  const [details, setDetails] = useState(initial);
  const [bank, setBank] = useState({ iban: "", accountName: "", bankName: "", swift: "", note: "" });
  const [busy, setBusy] = useState<null | "bank">(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const explain = (code: string) => t(`errors.${KNOWN_ERRORS.has(code) ? code : "generic"}` as "errors.generic");
  const address = Object.values(details.address ?? {}).filter(Boolean).join(", ");

  const askChange = async () => {
    setBusy("bank");
    setError(null);
    setNotice(null);
    const result = await actions.requestChange(bank);
    setBusy(null);
    if (!result.ok) return setError(result.code === "conflict" ? t("bank.alreadyOpen") : explain(result.code));
    setDetails((d) => ({ ...d, payment_hold: true, open_requests: [result.request, ...d.open_requests] }));
    setBank({ iban: "", accountName: "", bankName: "", swift: "", note: "" });
    setNotice(t("bank.requested", { reference: result.request.reference }));
  };

  const fact = (label: string, value: React.ReactNode) =>
    value ? (
      <div>
        <dt className="text-xs text-muted-foreground">{label}</dt>
        <dd>{value}</dd>
      </div>
    ) : null;

  return (
    <div className="space-y-10" data-vendor-details>
      <Link href="/vendor-portal" className="inline-flex items-center gap-1 text-sm text-primary underline-offset-4 hover:underline">
        <ArrowLeft className="size-4 rtl:rotate-180" aria-hidden />
        {t("detail.back")}
      </Link>
      <header className="space-y-2 border-b border-border pb-6">
        <p className="eyebrow">{t("details.eyebrow")}</p>
        <h1 className="font-display text-[2rem] leading-[1.1] text-balance sm:text-[2.6rem]">{details.legal_name}</h1>
        <p className="text-sm text-muted-foreground">{t("details.intro")}</p>
      </header>

      <p role="status" aria-live="polite" className="text-sm font-medium text-success-ink" data-vendor-notice>
        {notice ?? ""}
      </p>
      {error && (
        <p role="alert" className="text-sm text-danger-ink">
          {error}
        </p>
      )}

      <section aria-labelledby={`${ids}-verified`} className="space-y-3">
        <h2 id={`${ids}-verified`} className="flex items-center gap-2 font-display text-xl">
          <ShieldCheck className="size-5" aria-hidden />
          {t("details.verified")}
        </h2>
        <dl className="card-hairline grid grid-cols-1 gap-4 border-t-4 border-t-success p-5 text-sm sm:grid-cols-2 sm:p-6">
          {fact(t("details.legalName"), details.legal_name)}
          {fact(t("details.legalNameAr"), details.legal_name_ar ? <span dir="rtl">{details.legal_name_ar}</span> : null)}
          {fact(t("details.tradeName"), details.trade_name)}
          {fact(
            t("details.trn"),
            details.trn ? (
              <span>
                <span dir="ltr" className="font-mono">
                  {details.trn}
                </span>{" "}
                <Badge variant={details.trn_status === "verified" ? "success" : "outline"}>{t(`trn.${details.trn_status}` as "trn.none")}</Badge>
              </span>
            ) : null,
          )}
          {fact(t("details.address"), address || null)}
          {fact(t("details.phoneOnFile"), details.phone_masked ? (
              <span dir="ltr" className="font-mono">
                {details.phone_masked}
              </span>
            ) : null)}
        </dl>
        {details.payment_hold && (
          <p className="flex items-start gap-2 rounded-lg border border-warning-ink/35 bg-warning-soft p-4 text-sm text-warning-ink" data-vendor-hold>
            <ShieldAlert className="mt-0.5 size-4 shrink-0" aria-hidden />
            {t("details.hold")}
          </p>
        )}
      </section>

      <section aria-labelledby={`${ids}-bank`} className="space-y-3">
        <h2 id={`${ids}-bank`} className="flex items-center gap-2 font-display text-xl">
          <Landmark className="size-5" aria-hidden />
          {t("bank.title")}
        </h2>
        {details.account ? (
          <dl className="card-hairline grid grid-cols-1 gap-4 p-5 text-sm sm:grid-cols-2 sm:p-6" data-vendor-account>
            {fact(
              t("bank.iban"),
              <span dir="ltr" className="font-mono text-base font-medium">
                {details.account.iban_masked}
              </span>,
            )}
            {fact(t("bank.accountName"), details.account.account_name)}
            {fact(t("bank.bankName"), details.account.bank_name)}
            {fact(t("bank.swift"), details.account.swift ? (
                <span dir="ltr" className="font-mono">
                  {details.account.swift}
                </span>
              ) : null)}
            {fact(t("bank.since"), details.account.since ? format.dateTime(new Date(details.account.since), { dateStyle: "medium" }) : null)}
          </dl>
        ) : (
          <p className="text-sm text-muted-foreground">{t("bank.none")}</p>
        )}

        <div className="space-y-2" data-open-requests>
          <h3 className="font-medium">{t("bank.openRequests")}</h3>
          {details.open_requests.length === 0 ? (
            <p className="text-sm text-muted-foreground">{t("bank.noRequests")}</p>
          ) : (
            <ul className="space-y-2">
              {details.open_requests.map((r) => (
                <li key={r.reference} className="card-hairline p-4 text-sm" data-open-request={r.reference}>
                  <p className="font-medium">
                    <span dir="ltr">{r.reference}</span>
                    {r.iban_last4 && (
                      <span className="ms-2 text-muted-foreground">{t("bank.endingIn", { last4: r.iban_last4 })}</span>
                    )}
                  </p>
                  <p className="text-muted-foreground">
                    {t(`requestStatus.${r.status}` as "requestStatus.awaiting_verification")}
                    {" · "}
                    {format.dateTime(new Date(r.created_at), { dateStyle: "medium" })}
                  </p>
                </li>
              ))}
            </ul>
          )}
        </div>

        <form
          className="card-hairline space-y-3 p-5 sm:p-6"
          onSubmit={(e) => {
            e.preventDefault();
            void askChange();
          }}
          data-bank-change-form
        >
          <h3 className="font-medium">{t("bank.change")}</h3>
          <p className="text-sm text-muted-foreground" data-bank-explanation>
            {t("bank.explanation")}
          </p>
          <div className="grid gap-3 sm:grid-cols-2">
            {(
              [
                ["iban", t("bank.iban"), true, "ltr"],
                ["accountName", t("bank.accountName"), true, undefined],
                ["bankName", t("bank.bankName"), false, undefined],
                ["swift", t("bank.swift"), false, "ltr"],
              ] as const
            ).map(([key, label, required, dir]) => (
              <div key={key} className="space-y-1">
                <label htmlFor={`${ids}-${key}`} className="block text-sm font-medium">
                  {label}
                </label>
                <Input
                  id={`${ids}-${key}`}
                  value={bank[key]}
                  required={required}
                  dir={dir}
                  onChange={(e) => setBank((b) => ({ ...b, [key]: e.target.value }))}
                />
              </div>
            ))}
          </div>
          <label htmlFor={`${ids}-note`} className="block text-sm font-medium">
            {t("bank.note")}
          </label>
          <Textarea id={`${ids}-note`} rows={2} maxLength={1000} value={bank.note} onChange={(e) => setBank((b) => ({ ...b, note: e.target.value }))} />
          <Button type="submit" variant="outline" disabled={busy !== null || !bank.iban.trim() || !bank.accountName.trim()}>
            {busy === "bank" && <Loader2 className="animate-spin" aria-hidden />}
            {t("bank.submit")}
          </Button>
        </form>
      </section>

      <section aria-labelledby={`${ids}-contacts`} className="space-y-3">
        <h2 id={`${ids}-contacts`} className="flex items-center gap-2 font-display text-xl">
          <Users className="size-5" aria-hidden />
          {t("details.contacts")}
        </h2>
        <p className="text-sm text-muted-foreground">{t("details.contactsIntro")}</p>
        {details.contacts.length === 0 ? (
          <p className="text-sm text-muted-foreground">{t("details.noContacts")}</p>
        ) : (
          <ul className="space-y-3" data-contacts>
            {details.contacts.map((c, index) => (
              <li key={index} className="card-hairline p-4 text-sm" data-contact-row={index}>
                <dl className="grid gap-2 sm:grid-cols-2">
                  {fact(t("details.name"), c.name)}
                  {fact(t("details.role"), c.role)}
                  {fact(t("details.email"), c.email ? <span dir="ltr">{c.email}</span> : null)}
                  {fact(t("details.phone"), c.phone ? <span dir="ltr">{c.phone}</span> : null)}
                </dl>
              </li>
            ))}
          </ul>
        )}
        <p className="text-xs text-muted-foreground">{t("details.phoneNote")}</p>
      </section>
    </div>
  );
}
