// SPDX-License-Identifier: AGPL-3.0-only
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
]);

export function ErrorText({ code, className }: { code: string | null; className?: string }) {
  const t = useTranslations("Pay.errors");
  const tErrors = useTranslations("Errors");
  if (!code) return null;
  return (
    <p role="alert" className={cn("text-sm text-destructive", className)} data-error={code}>
      {PAY_CODES.has(code) ? t(code as "vendor_duplicate") : tErrors(errorKey(code) as "generic")}
    </p>
  );
}

export function VendorStatusBadge({ status }: { status: string }) {
  const t = useTranslations("Pay.vendorStatus");
  return (
    <Badge variant={status === "active" ? "default" : status === "pending" ? "signal" : "outline"} data-vendor-status={status}>
      {t(status as "active")}
    </Badge>
  );
}

export function TicketStatusBadge({ status }: { status: string }) {
  const t = useTranslations("Pay.ticketStatus");
  const open = status === "awaiting_verification" || status === "awaiting_approval" || status === "cooling_off";
  return (
    <Badge variant={open ? "signal" : status === "completed" ? "default" : "outline"} data-ticket-status={status}>
      {t(status as "completed")}
    </Badge>
  );
}

/** "Payments on hold" with the reason, shown wherever a vendor appears. */
export function HoldBadge({ reason }: { reason: string | null | undefined }) {
  const t = useTranslations("Pay.hold");
  return (
    <Badge variant="signal" data-hold={reason ?? "hold"}>
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
}: { title: React.ReactNode; children: React.ReactNode; className?: string } & Record<`data-${string}`, string | boolean>) {
  return (
    <section className={cn("space-y-3 rounded-xl border bg-surface p-5", className)} {...rest}>
      <h2 className="font-display text-xl">{title}</h2>
      {children}
    </section>
  );
}
