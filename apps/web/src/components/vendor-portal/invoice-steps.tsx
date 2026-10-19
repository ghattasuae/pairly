// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
"use client";

import { Check, Circle, CircleDot, XCircle } from "lucide-react";
import { useFormatter, useTranslations } from "next-intl";

import { cn } from "@/lib/utils";
import { stepViews, type VendorPortalInvoice } from "@/lib/vendor-portal/types";

/**
 * Where an invoice stands: received, in review, approved, scheduled, paid (a credit note:
 * received, in review, applied), each with its date. Not colour alone: every state has an
 * icon and words.
 */
export function InvoiceSteps({ invoice, compact = false }: { invoice: VendorPortalInvoice; compact?: boolean }) {
  const t = useTranslations("VendorPortal");
  const format = useFormatter();
  const steps = stepViews(invoice);
  return (
    <ol
      className={cn("grid", compact ? "grid-cols-1 gap-3 sm:grid-cols-5 sm:gap-2" : "grid-cols-1 gap-0")}
      data-invoice-steps={invoice.id}
    >
      {steps.map((s, i) => {
        const Icon = s.step === "rejected" ? XCircle : s.state === "done" ? Check : s.state === "current" ? CircleDot : Circle;
        const last = i === steps.length - 1;
        return (
          <li
            key={s.step}
            className={cn(
              "relative flex gap-2.5",
              compact ? "sm:flex-col sm:gap-1.5 sm:border-t-2 sm:pt-2" : "pb-5 last:pb-0",
              compact && (s.state === "done" ? "sm:border-success" : s.state === "current" ? "sm:border-foreground" : "sm:border-rule"),
              compact && s.step === "rejected" && "sm:border-destructive",
              s.state === "ahead" && "text-muted-foreground",
            )}
            data-step={s.step}
            data-state={s.state}
            aria-current={s.state === "current" ? "step" : undefined}
          >
            {!compact && !last && <span aria-hidden className="absolute start-[0.6875rem] top-6 bottom-0 w-px bg-rule" />}
            <span
              aria-hidden
              className={cn(
                "relative flex size-6 shrink-0 items-center justify-center rounded-full border-2",
                s.step === "rejected"
                  ? "border-destructive bg-danger-soft text-danger-ink"
                  : s.state === "done"
                    ? "border-success bg-success text-background"
                    : s.state === "current"
                      ? "border-foreground bg-surface text-foreground"
                      : "border-input bg-surface text-muted-foreground",
              )}
            >
              <Icon className="size-3.5" />
            </span>
            <div className="min-w-0">
              <p className={cn("text-sm", s.state === "current" && "font-semibold", s.step === "rejected" && "text-danger-ink")}>
                {t(`status.${s.step}` as "status.received")}
                <span className="sr-only">: {t(`state.${s.state}`)}</span>
              </p>
              {s.at && <p className="font-mono text-xs text-muted-foreground">{format.dateTime(new Date(s.at), { dateStyle: "medium" })}</p>}
            </div>
          </li>
        );
      })}
    </ol>
  );
}
