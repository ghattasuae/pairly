// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
"use client";

import { ArrowLeft, Loader2, Send } from "lucide-react";
import { useFormatter, useTranslations } from "next-intl";
import { useId, useState } from "react";

import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/form-controls";
import { Link } from "@/i18n/navigation";
import { writeToAccountsPayable } from "@/lib/actions/vendor-portal";
import type { VendorPortalInvoiceDetail } from "@/lib/vendor-portal/types";

import { InvoiceSteps } from "./invoice-steps";
import { money } from "./money";

const KNOWN_ERRORS = new Set(["not_signed_in", "rate_limited", "conflict", "invalid_input"]);

/**
 * One invoice or credit note in the vendor portal: where it stands with the date of each
 * step, the amounts and dates, and the messages with accounts payable about it (write
 * here; AP answers here and by email).
 */
export function VendorInvoiceDetail({
  invoice: initial,
  write = writeToAccountsPayable,
}: {
  invoice: VendorPortalInvoiceDetail;
  write?: typeof writeToAccountsPayable;
}) {
  const t = useTranslations("VendorPortal");
  const format = useFormatter();
  const ids = useId();
  const [invoice, setInvoice] = useState(initial);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const date = (value: string | null | undefined) => (value ? format.dateTime(new Date(value), { dateStyle: "medium" }) : null);

  const send = async () => {
    if (!text.trim()) return;
    setBusy(true);
    setError(null);
    const result = await write(invoice.id, text.trim());
    setBusy(false);
    if (!result.ok) return setError(t(`errors.${KNOWN_ERRORS.has(result.code) ? result.code : "generic"}` as "errors.generic"));
    setInvoice(result.invoice);
    setText("");
    setNotice(t("detail.sent"));
  };

  const facts: Array<[string, string | null]> = [
    [t("detail.invoiceNumber"), invoice.invoice_number ?? null],
    [t("detail.invoiceDate"), date(invoice.invoice_date)],
    [t("home.dueDate"), date(invoice.due_date)],
    [invoice.status === "paid" ? t("home.paidOn") : t("home.expected"), date(invoice.expected_payment_date)],
    [t("detail.total"), money(format, invoice.total, invoice.currency) || null],
    [t("home.toPay"), Number(invoice.credit_total) > 0 ? money(format, invoice.amount_to_pay, invoice.currency) : null],
    [t("detail.paymentReference"), invoice.payment_reference ?? null],
    [t("detail.creditFor"), invoice.credit_for ?? null],
  ];

  return (
    <article aria-labelledby={`${ids}-title`} className="space-y-8" data-vendor-invoice-detail={invoice.id} data-status={invoice.status}>
      <Link href="/vendor-portal" className="inline-flex items-center gap-1 text-sm text-primary underline-offset-4 hover:underline">
        <ArrowLeft className="size-4 rtl:rotate-180" aria-hidden />
        {t("detail.back")}
      </Link>
      <header className="space-y-1">
        <p className="eyebrow">{t(`kind.${invoice.document_type === "credit_note" ? "credit_note" : "invoice"}`)}</p>
        <h1 id={`${ids}-title`} className="font-display text-3xl leading-tight">
          <span dir="ltr">{invoice.invoice_number ?? invoice.reference}</span>
        </h1>
        <p className="text-sm text-muted-foreground">
          <span dir="ltr">{invoice.reference}</span>
          {" · "}
          {t("detail.receivedOn", { date: date(invoice.received_at) ?? "" })}
        </p>
      </header>

      <section aria-labelledby={`${ids}-status`} className="space-y-3">
        <h2 id={`${ids}-status`} className="font-display text-xl">
          {t("detail.status")}
        </h2>
        <InvoiceSteps invoice={invoice} />
        <p className="text-sm text-muted-foreground">{t(`help.${invoice.status}` as "help.received")}</p>
      </section>

      <section aria-labelledby={`${ids}-facts`} className="space-y-3">
        <h2 id={`${ids}-facts`} className="font-display text-xl">
          {t("detail.facts")}
        </h2>
        <dl className="grid grid-cols-1 gap-3 rounded-2xl border bg-surface p-5 text-sm sm:grid-cols-2">
          {facts
            .filter(([, value]) => value)
            .map(([label, value]) => (
              <div key={label}>
                <dt className="text-xs text-muted-foreground">{label}</dt>
                <dd>{value}</dd>
              </div>
            ))}
        </dl>
      </section>

      <section aria-labelledby={`${ids}-messages`} className="space-y-3">
        <h2 id={`${ids}-messages`} className="font-display text-xl">
          {t("detail.messages")}
        </h2>
        <p role="status" aria-live="polite" className="text-sm font-medium" data-vendor-notice>
          {notice ?? ""}
        </p>
        {error && (
          <p role="alert" className="text-sm text-destructive">
            {error}
          </p>
        )}
        {invoice.thread.length === 0 ? (
          <p className="text-sm text-muted-foreground">{t("detail.noMessages")}</p>
        ) : (
          <ol className="space-y-3">
            {invoice.thread.map((m) => (
              <li
                key={m.id}
                className={m.from_vendor ? "ms-6 rounded-xl border bg-accent/40 p-4" : "me-6 rounded-xl border bg-surface p-4"}
                data-message={m.from_vendor ? "from_vendor" : "from_ap"}
              >
                <p className="text-xs text-muted-foreground">
                  {m.from_vendor ? m.author || t("detail.fromYou") : t("detail.fromAp")}
                  {" · "}
                  {format.dateTime(new Date(m.at), { dateStyle: "medium", timeStyle: "short" })}
                </p>
                <p className="mt-1 text-sm whitespace-pre-wrap">{m.text}</p>
              </li>
            ))}
          </ol>
        )}
        {invoice.can_message && (
          <form
            className="space-y-2"
            onSubmit={(e) => {
              e.preventDefault();
              void send();
            }}
          >
            <label htmlFor={`${ids}-text`} className="block text-sm font-medium">
              {t("detail.writeLabel")}
            </label>
            <Textarea id={`${ids}-text`} value={text} onChange={(e) => setText(e.target.value)} rows={3} maxLength={4000} />
            <Button type="submit" disabled={busy || !text.trim()}>
              {busy ? <Loader2 className="animate-spin" aria-hidden /> : <Send className="rtl:-scale-x-100" aria-hidden />}
              {t("detail.send")}
            </Button>
          </form>
        )}
      </section>
    </article>
  );
}
