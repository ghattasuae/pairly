// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
"use client";

import { Loader2, Scale } from "lucide-react";
import { useFormatter, useTranslations } from "next-intl";
import { useId, useState } from "react";

import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/form-controls";
import { askForHumanReview } from "@/lib/actions/vendor-portal";
import type { VendorPortalDecision, VendorPortalInvoiceDetail } from "@/lib/vendor-portal/types";

const KNOWN_ERRORS = new Set(["not_signed_in", "rate_limited", "conflict", "invalid_input", "not_found"]);

/**
 * The decisions about one invoice the vendor may ask a person to review (a duplicate
 * flag, a rejection, a bank-detail change step), each with its review once asked: the
 * reference, the date a person answers by and, when resolved, the outcome.
 */
export function VendorInvoiceDecisions({
  invoice,
  onChange,
  ask = askForHumanReview,
}: {
  invoice: VendorPortalInvoiceDetail;
  onChange: (invoice: VendorPortalInvoiceDetail) => void;
  ask?: typeof askForHumanReview;
}) {
  const t = useTranslations("VendorPortal.review");
  const errors = useTranslations("VendorPortal.errors");
  const format = useFormatter();
  const ids = useId();
  const [open, setOpen] = useState<string | null>(null);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const date = (value: string) => format.dateTime(new Date(value), { dateStyle: "medium" });

  if (invoice.decisions.length === 0) return null;

  const send = async (decision: VendorPortalDecision) => {
    if (!reason.trim()) return;
    setBusy(true);
    setError(null);
    const result = await ask(invoice.id, decision.decision_id, reason.trim());
    setBusy(false);
    if (!result.ok) {
      const code = KNOWN_ERRORS.has(result.code) ? result.code : "generic";
      return setError(code === "not_found" ? t("notFound") : errors(code as "generic"));
    }
    onChange(result.invoice);
    setOpen(null);
    setReason("");
    setNotice(t("sent"));
  };

  return (
    <section aria-labelledby={`${ids}-title`} className="space-y-3" data-vendor-decisions>
      <h2 id={`${ids}-title`} className="font-display text-xl">
        {t("title")}
      </h2>
      <p className="text-sm text-muted-foreground">{t("intro")}</p>
      <p role="status" aria-live="polite" className="text-sm font-medium">
        {notice ?? ""}
      </p>
      {error && (
        <p role="alert" className="text-sm text-destructive">
          {error}
        </p>
      )}
      <ul className="space-y-3">
        {invoice.decisions.map((d) => (
          <li key={d.decision_id} className="space-y-2 rounded-xl border bg-surface p-4" data-decision={d.decision_id} data-kind={d.kind}>
            <p className="text-sm font-medium">{t(`kind.${d.kind}`)}</p>
            <p className="text-xs text-muted-foreground">{t("decidedOn", { date: date(d.decided_at) })}</p>
            {d.appeal ? (
              <p className="text-sm" data-appeal-status={d.appeal.status}>
                {d.appeal.status === "resolved"
                  ? t(`outcome.${d.appeal.outcome ?? "upheld"}` as "outcome.upheld", { reference: d.appeal.reference })
                  : t("asked", { reference: d.appeal.reference, date: date(d.appeal.sla_due_at) })}
              </p>
            ) : open === d.decision_id ? (
              <form
                className="space-y-2"
                onSubmit={(e) => {
                  e.preventDefault();
                  void send(d);
                }}
              >
                <label htmlFor={`${ids}-${d.decision_id}`} className="block text-sm font-medium">
                  {t("reasonLabel")}
                </label>
                <Textarea
                  id={`${ids}-${d.decision_id}`}
                  value={reason}
                  onChange={(e) => setReason(e.target.value)}
                  rows={3}
                  maxLength={4000}
                />
                <div className="flex flex-wrap gap-2">
                  <Button type="submit" disabled={busy || !reason.trim()}>
                    {busy ? <Loader2 className="animate-spin" aria-hidden /> : <Scale aria-hidden />}
                    {t("send")}
                  </Button>
                  <Button type="button" variant="ghost" onClick={() => setOpen(null)} disabled={busy}>
                    {t("cancel")}
                  </Button>
                </div>
              </form>
            ) : (
              d.can_appeal && (
                <Button
                  type="button"
                  variant="outline"
                  onClick={() => {
                    setOpen(d.decision_id);
                    setReason("");
                    setNotice(null);
                  }}
                >
                  {t("ask")}
                </Button>
              )
            )}
          </li>
        ))}
      </ul>
    </section>
  );
}
