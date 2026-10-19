// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
"use client";

import { ArrowRight, FileUp, Loader2, MessageSquare, ShieldCheck } from "lucide-react";
import { useFormatter, useTranslations } from "next-intl";
import { useId, useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Select } from "@/components/ui/form-controls";
import { Link } from "@/i18n/navigation";
import { UPLOAD_HEADER } from "@/lib/upload-guard";
import type { VendorPortalInvoice, VendorPortalMe } from "@/lib/vendor-portal/types";

import { InvoiceSteps } from "./invoice-steps";
import { money } from "./money";

const KNOWN_ERRORS = new Set(["not_signed_in", "rate_limited", "conflict", "too_large", "invalid_input", "unsupported_media_type"]);
// Invoices a credit note may name: the vendor's own, not paid, not closed.
const CREDITABLE = new Set(["received", "in_review", "approved"]);

export type SubmitResult = { ok: true; invoice: VendorPortalInvoice; duplicate: boolean } | { ok: false; code: string };

export async function submitDocument(file: File, kind: "invoice" | "credit_note", creditFor: string | null): Promise<SubmitResult> {
  const body = new FormData();
  body.set("file", file);
  body.set("document_type", kind);
  if (kind === "credit_note" && creditFor) body.set("credit_for", creditFor);
  try {
    const response = await fetch("/api/vendor-portal/invoices", {
      method: "POST",
      body,
      headers: { [UPLOAD_HEADER]: "1" },
      credentials: "same-origin",
    });
    const data = (await response.json().catch(() => ({}))) as {
      code?: string;
      invoice?: VendorPortalInvoice;
      duplicate?: boolean;
    };
    if (response.ok && data.invoice) return { ok: true, invoice: data.invoice, duplicate: Boolean(data.duplicate) };
    return { ok: false, code: data.code ?? "unavailable" };
  } catch {
    return { ok: false, code: "unavailable" };
  }
}

/**
 * The vendor portal's home (brief 5.1 item 11): what needs the vendor now, every invoice
 * and credit note with its status, dates and expected payment date, and a form to send a
 * new invoice or credit note. Mobile first, one column.
 */
export function VendorPortalHome({
  me,
  invoices: initial,
  submit = submitDocument,
}: {
  me: VendorPortalMe;
  invoices: VendorPortalInvoice[];
  submit?: typeof submitDocument;
}) {
  const t = useTranslations("VendorPortal");
  const format = useFormatter();
  const ids = useId();
  const [invoices, setInvoices] = useState(initial);
  const [kind, setKind] = useState<"invoice" | "credit_note">("invoice");
  const [creditFor, setCreditFor] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const creditable = invoices.filter((i) => i.document_type === "invoice" && CREDITABLE.has(i.status));
  const upcoming = invoices
    .filter((i) => (i.status === "approved" || i.status === "scheduled") && i.expected_payment_date)
    .sort((a, b) => String(a.expected_payment_date).localeCompare(String(b.expected_payment_date)));
  const inReview = invoices.filter((i) => i.status === "received" || i.status === "in_review").length;

  const send = async () => {
    if (!file) return;
    if (kind === "credit_note" && !creditFor) return setError(t("submit.chooseInvoice"));
    setBusy(true);
    setError(null);
    setNotice(null);
    const result = await submit(file, kind, kind === "credit_note" ? creditFor : null);
    setBusy(false);
    if (!result.ok) {
      return setError(t(`errors.${KNOWN_ERRORS.has(result.code) ? result.code : "generic"}` as "errors.generic"));
    }
    setInvoices((list) => [result.invoice, ...list.filter((i) => i.id !== result.invoice.id)]);
    setNotice(result.duplicate ? t("submit.duplicate", { reference: result.invoice.reference }) : t("submit.sent", { reference: result.invoice.reference }));
    setFile(null);
    setCreditFor("");
  };

  return (
    <div className="space-y-10" data-vendor-home>
      <header className="space-y-2">
        <p className="eyebrow">{me.organisation}</p>
        <h1 className="font-display text-3xl leading-tight sm:text-4xl">{me.vendor_name}</h1>
        <p className="text-sm text-muted-foreground">
          {t("home.signedInAs", { name: me.contact_name || me.email })}
          {" · "}
          <span dir="ltr">{me.vendor_reference}</span>
        </p>
      </header>

      <section aria-labelledby={`${ids}-now`} className="grid gap-3 sm:grid-cols-3" data-vendor-summary>
        <h2 id={`${ids}-now`} className="sr-only">
          {t("home.now")}
        </h2>
        <div className="rounded-2xl border bg-surface p-4">
          <p className="text-xs text-muted-foreground">{t("home.inReview")}</p>
          <p className="font-display text-2xl">{format.number(inReview)}</p>
        </div>
        <div className="rounded-2xl border bg-surface p-4 sm:col-span-2">
          <p className="text-xs text-muted-foreground">{t("home.nextPayment")}</p>
          {upcoming[0] ? (
            <p className="font-display text-xl">
              {money(format, upcoming[0].amount_to_pay ?? upcoming[0].total, upcoming[0].currency)}
              <span className="ms-2 text-sm text-muted-foreground">
                {t("home.expectedOn", { date: format.dateTime(new Date(String(upcoming[0].expected_payment_date)), { dateStyle: "medium" }) })}
              </span>
            </p>
          ) : (
            <p className="text-sm text-muted-foreground">{t("home.noPayment")}</p>
          )}
        </div>
        <Link
          href="/vendor-portal/details"
          className="flex items-center gap-2 rounded-2xl border bg-surface p-4 text-sm font-medium text-primary underline-offset-4 hover:underline sm:col-span-3"
          data-vendor-details-link
        >
          <ShieldCheck className="size-4" aria-hidden />
          {t("home.details")}
          <ArrowRight className="ms-auto size-4 rtl:rotate-180" aria-hidden />
        </Link>
      </section>

      <section aria-labelledby={`${ids}-submit`} className="space-y-3 rounded-2xl border bg-surface p-5" data-vendor-submit>
        <h2 id={`${ids}-submit`} className="flex items-center gap-2 font-display text-xl">
          <FileUp className="size-5" aria-hidden />
          {t("submit.title")}
        </h2>
        <p className="text-sm text-muted-foreground">{t("submit.intro")}</p>
        <form
          className="space-y-3"
          onSubmit={(e) => {
            e.preventDefault();
            void send();
          }}
        >
          <fieldset className="flex flex-wrap gap-4">
            <legend className="sr-only">{t("submit.kind")}</legend>
            {(["invoice", "credit_note"] as const).map((k) => (
              <label key={k} className="inline-flex items-center gap-2 text-sm">
                <input type="radio" name={`${ids}-kind`} value={k} checked={kind === k} onChange={() => setKind(k)} />
                {t(`kind.${k}`)}
              </label>
            ))}
          </fieldset>
          {kind === "credit_note" && (
            <div className="space-y-1">
              <label htmlFor={`${ids}-for`} className="block text-sm font-medium">
                {t("submit.creditFor")}
              </label>
              <Select id={`${ids}-for`} value={creditFor} onChange={(e) => setCreditFor(e.target.value)} required>
                <option value="">{t("submit.chooseInvoice")}</option>
                {creditable.map((i) => (
                  <option key={i.id} value={i.id}>
                    {i.invoice_number ?? i.reference} {i.total ? `(${money(format, i.total, i.currency)})` : ""}
                  </option>
                ))}
              </Select>
              {creditable.length === 0 && <p className="text-xs text-muted-foreground">{t("submit.noCreditable")}</p>}
            </div>
          )}
          <div className="space-y-1">
            <label htmlFor={`${ids}-file`} className="block text-sm font-medium">
              {t("submit.file")}
            </label>
            <input
              id={`${ids}-file`}
              type="file"
              accept=".pdf,.png,.jpg,.jpeg,.xml,.docx"
              onChange={(e) => setFile(e.target.files?.[0] ?? null)}
              className="block w-full text-sm file:me-3 file:rounded-md file:border file:border-input file:bg-surface file:px-3 file:py-1.5"
            />
          </div>
          <p role="status" aria-live="polite" className="text-sm font-medium" data-vendor-notice>
            {notice ?? ""}
          </p>
          {error && (
            <p role="alert" className="text-sm text-destructive">
              {error}
            </p>
          )}
          <Button type="submit" disabled={busy || !file}>
            {busy ? <Loader2 className="animate-spin" aria-hidden /> : <FileUp aria-hidden />}
            {busy ? t("submit.sending") : t("submit.send")}
          </Button>
        </form>
      </section>

      <section aria-labelledby={`${ids}-invoices`} className="space-y-4">
        <h2 id={`${ids}-invoices`} className="font-display text-2xl">
          {t("home.invoices")}
        </h2>
        {invoices.length === 0 ? (
          <p className="text-sm text-muted-foreground">{t("home.noInvoices")}</p>
        ) : (
          <ul className="space-y-4">
            {invoices.map((invoice) => (
              <li key={invoice.id}>
                <article
                  aria-labelledby={`inv-${invoice.id}`}
                  className="space-y-4 rounded-2xl border bg-surface p-5 shadow-soft"
                  data-vendor-invoice={invoice.id}
                  data-status={invoice.status}
                >
                  <div className="flex flex-wrap items-start justify-between gap-2">
                    <div className="min-w-0 space-y-1">
                      <h3 id={`inv-${invoice.id}`} className="font-display text-lg leading-snug">
                        <span dir="ltr">{invoice.invoice_number ?? invoice.reference}</span>
                      </h3>
                      <p className="text-xs text-muted-foreground">
                        {t(`kind.${invoice.document_type === "credit_note" ? "credit_note" : "invoice"}`)}
                        {" · "}
                        <span dir="ltr">{invoice.reference}</span>
                        {invoice.credit_for && (
                          <>
                            {" · "}
                            {t("home.creditFor", { reference: invoice.credit_for })}
                          </>
                        )}
                      </p>
                    </div>
                    <div className="text-end">
                      <p className="font-medium">{money(format, invoice.total, invoice.currency)}</p>
                      <Badge variant={invoice.status === "rejected" ? "outline" : invoice.status === "paid" ? "default" : "signal"}>{t(`status.${invoice.status}` as "status.received")}</Badge>
                    </div>
                  </div>
                  <InvoiceSteps invoice={invoice} compact />
                  <dl className="grid grid-cols-2 gap-2 text-sm sm:grid-cols-3">
                    {invoice.due_date && (
                      <div>
                        <dt className="text-xs text-muted-foreground">{t("home.dueDate")}</dt>
                        <dd>{format.dateTime(new Date(invoice.due_date), { dateStyle: "medium" })}</dd>
                      </div>
                    )}
                    {invoice.expected_payment_date && (
                      <div>
                        <dt className="text-xs text-muted-foreground">{invoice.status === "paid" ? t("home.paidOn") : t("home.expected")}</dt>
                        <dd data-expected-payment>{format.dateTime(new Date(invoice.expected_payment_date), { dateStyle: "medium" })}</dd>
                      </div>
                    )}
                    {Number(invoice.credit_total) > 0 && (
                      <div>
                        <dt className="text-xs text-muted-foreground">{t("home.toPay")}</dt>
                        <dd>{money(format, invoice.amount_to_pay, invoice.currency)}</dd>
                      </div>
                    )}
                  </dl>
                  <div className="flex flex-wrap items-center gap-2">
                    {invoice.messages > 0 && (
                      <Badge>
                        <MessageSquare className="size-3" aria-hidden />
                        {t("home.messages", { count: invoice.messages })}
                      </Badge>
                    )}
                    <Link
                      href={`/vendor-portal/invoices/${invoice.id}`}
                      className="ms-auto inline-flex items-center gap-1 text-sm font-medium text-primary underline-offset-4 hover:underline"
                      data-open-invoice={invoice.id}
                    >
                      {t("home.open")}
                      <ArrowRight className="size-4 rtl:rotate-180" aria-hidden />
                    </Link>
                  </div>
                </article>
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}
