// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
"use client";

import { ShieldAlert } from "lucide-react";
import { useTranslations } from "next-intl";
import type * as React from "react";

import { Badge } from "@/components/ui/badge";
import { errorKey } from "@/lib/action-result";
import { cn } from "@/lib/utils";

/** Error codes the Pay screens explain in their own words (else the shared ones). */
const PAY_CODES = new Set([
  "bank_change_already_open",
  "bank_change_state",
  "bank_details_invalid",
  "bank_details_unchanged",
  "no_phone_on_file",
  "phone_invalid",
  "trn_invalid",
  "vendor_duplicate",
  "vendor_not_open",
  "segregation_of_duties",
  "not_approvable",
  "export_refused",
  "nothing_to_pay",
  "cash_limit_required",
  "invalid_purchase_data",
  "number_taken",
  "purchase_order_state",
  "recipient_not_allowed",
  "invalid_statement",
  "statement_already_imported",
  "callback_source_required",
  "callback_source_missing",
  "journal_unbalanced",
  "journal_account_unmapped",
  "journal_value_unrepresentable",
]);

export function ErrorText({ code, className }: { code: string | null; className?: string }) {
  const t = useTranslations("Pay.errors");
  const tErrors = useTranslations("Errors");
  if (!code) return null;
  return (
    <p role="alert" className={cn("text-sm text-danger-ink", className)} data-error={code}>
      {PAY_CODES.has(code) ? t(code as "vendor_duplicate") : tErrors(errorKey(code) as "generic")}
    </p>
  );
}

export function VendorStatusBadge({ status }: { status: string }) {
  const t = useTranslations("Pay.vendorStatus");
  return (
    <Badge
      variant={status === "active" ? "success" : status === "pending" ? "warning" : status === "blocked" ? "danger" : "outline"}
      data-vendor-status={status}
    >
      {t(status as "active")}
    </Badge>
  );
}

export function TicketStatusBadge({ status }: { status: string }) {
  const t = useTranslations("Pay.ticketStatus");
  // The fraud defence reads in danger and warning, never in the saffron of an action:
  // locked until the call back (danger), then waiting on people or the clock (warning).
  const variant =
    status === "awaiting_verification"
      ? "danger"
      : status === "awaiting_approval" || status === "cooling_off"
        ? "warning"
        : status === "completed"
          ? "success"
          : "outline";
  return (
    <Badge variant={variant} data-ticket-status={status}>
      {t(status as "completed")}
    </Badge>
  );
}

/** "Payments on hold" with the reason, shown wherever a vendor appears. */
export function HoldBadge({ reason }: { reason: string | null | undefined }) {
  const t = useTranslations("Pay.hold");
  return (
    <Badge variant="danger" data-hold={reason ?? "hold"}>
      <ShieldAlert aria-hidden className="size-3" />
      {t("badge")}
      {reason ? `: ${t(`reason.${reason}` as "reason.manual")}` : ""}
    </Badge>
  );
}

export function Panel({
  title,
  children,
  className,
  ...rest
}: { title: React.ReactNode; children: React.ReactNode; className?: string | undefined } & Record<`data-${string}`, string | boolean>) {
  return (
    <section className={cn("card-hairline space-y-3 p-5 sm:p-6", className)} {...rest}>
      <h2 className="font-display text-xl leading-tight">{title}</h2>
      {children}
    </section>
  );
}
