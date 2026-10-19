// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
"use client";

import { BookCheck } from "lucide-react";
import { useTranslations } from "next-intl";
import { useId } from "react";

import { Select } from "@/components/ui/form-controls";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import type { PayTicketDetail } from "@/lib/pay/types";

export const CALLBACK_SOURCES = ["trade_licence", "signed_contract", "prior_correspondence", "official_registry"] as const;
export type CallbackSourceName = (typeof CALLBACK_SOURCES)[number];

export interface CallbackValue {
  phone: string;
  source: CallbackSourceName | "";
  reference: string;
}

export const EMPTY_CALLBACK: CallbackValue = { phone: "", source: "", reference: "" };

export function callbackComplete(value: CallbackValue, withPhone = true): boolean {
  return (!withPhone || value.phone.trim().length >= 5) && value.source !== "" && value.reference.trim().length >= 3;
}

/**
 * Where the call-back number comes from (security review 2026-10, M-05 and M-03):
 * accounts payable takes it from a source that does not depend on whoever sent the bank
 * details (the trade licence, a signed contract, earlier correspondence, an official
 * registry) and names it. The number a vendor's own form gave is never called.
 */
export function CallbackSourceFields({
  value,
  onChange,
  withPhone = true,
}: {
  value: CallbackValue;
  onChange: (v: CallbackValue) => void;
  withPhone?: boolean;
}) {
  const t = useTranslations("Pay.callback");
  const ids = useId();
  return (
    <fieldset className="space-y-3 rounded-lg border p-3" data-callback-source>
      <legend className="px-1 text-sm font-medium">{t("title")}</legend>
      <p className="text-sm text-muted-foreground">{t("intro")}</p>
      <div className="grid gap-3 sm:grid-cols-2">
        {withPhone && (
          <div className="space-y-1.5">
            <Label htmlFor={`${ids}-phone`}>{t("phone")}</Label>
            <Input
              id={`${ids}-phone`}
              type="tel"
              dir="ltr"
              required
              maxLength={40}
              value={value.phone}
              onChange={(e) => onChange({ ...value, phone: e.target.value })}
              data-callback-phone
            />
          </div>
        )}
        <div className="space-y-1.5">
          <Label htmlFor={`${ids}-source`}>{t("source")}</Label>
          <Select
            id={`${ids}-source`}
            required
            value={value.source}
            onChange={(e) => onChange({ ...value, source: e.target.value as CallbackSourceName | "" })}
            data-callback-source-type
          >
            <option value="">{t("choose")}</option>
            {CALLBACK_SOURCES.map((s) => (
              <option key={s} value={s}>
                {t(`sources.${s}`)}
              </option>
            ))}
          </Select>
        </div>
        <div className="space-y-1.5 sm:col-span-2">
          <Label htmlFor={`${ids}-reference`}>{t("reference")}</Label>
          <Input
            id={`${ids}-reference`}
            required
            minLength={3}
            maxLength={500}
            value={value.reference}
            onChange={(e) => onChange({ ...value, reference: e.target.value })}
            aria-describedby={`${ids}-reference-hint`}
            data-callback-reference
          />
          <p id={`${ids}-reference-hint`} className="text-xs text-muted-foreground">
            {t("referenceHint")}
          </p>
        </div>
      </div>
    </fieldset>
  );
}

/** Where the number on a ticket came from, and whether it matched what the request said. */
export function CallbackSourceView({ ticket }: { ticket: PayTicketDetail }) {
  const t = useTranslations("Pay.callback");
  const callback = ticket.verification.callback;
  if (!callback) return null;
  return (
    <div className="flex items-start gap-3 rounded-lg border border-border bg-surface-sunken p-3 text-sm" data-callback-view>
      <BookCheck aria-hidden className="mt-0.5 size-5 shrink-0 text-info" />
      <div className="space-y-1">
        <p>
          <span className="font-medium">{t("from")}</span> {t(`sources.${callback.source as CallbackSourceName}`)}
        </p>
        {callback.reference && (
          <p className="text-muted-foreground">
            <bdi>{callback.reference}</bdi>
          </p>
        )}
        {callback.matches_claim === false && (
          <p className="font-medium text-danger-ink" data-callback-differs>
            {t("differs")}
          </p>
        )}
        {callback.matches_claim === true && (
          <p className="text-success-ink" data-callback-matches>
            {t("matches")}
          </p>
        )}
      </div>
    </div>
  );
}
