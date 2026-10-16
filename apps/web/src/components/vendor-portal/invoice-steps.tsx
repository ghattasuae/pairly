// SPDX-License-Identifier: AGPL-3.0-only
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
    <ol className={cn("grid gap-3", compact ? "grid-cols-1 sm:grid-cols-5" : "grid-cols-1")} data-invoice-steps={invoice.id}>
      {steps.map((s) => {
        const Icon = s.step === "rejected" ? XCircle : s.state === "done" ? Check : s.state === "current" ? CircleDot : Circle;
        return (
          <li
            key={s.step}
            className={cn("flex gap-2", s.state === "ahead" && "text-muted-foreground")}
            data-step={s.step}
            data-state={s.state}
            aria-current={s.state === "current" ? "step" : undefined}
          >
            <Icon
              className={cn("mt-0.5 size-4 shrink-0", s.state === "current" ? "text-primary" : s.state === "ahead" ? "text-muted-foreground" : "")}
              aria-hidden
            />
            <div className="min-w-0">
              <p className={cn("text-sm", s.state === "current" && "font-semibold")}>
                {t(`status.${s.step}` as "status.received")}
                <span className="sr-only">: {t(`state.${s.state}`)}</span>
              </p>
              {s.at && <p className="text-xs text-muted-foreground">{format.dateTime(new Date(s.at), { dateStyle: "medium" })}</p>}
            </div>
          </li>
        );
      })}
    </ol>
  );
}
