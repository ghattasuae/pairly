// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
"use client";

import { Check, Loader2 } from "lucide-react";
import { useTranslations } from "next-intl";
import { useId, useState } from "react";
import type * as React from "react";

import { StepUpPrompt } from "@/components/states/step-up-prompt";
import { Button } from "@/components/ui/button";
import { Checkbox, FieldRow } from "@/components/ui/form-controls";
import { Input } from "@/components/ui/input";
import { savePaySettings, type PaySettingsInput } from "@/lib/actions/pay";
import type { PaySettings } from "@/lib/pay/types";

import { ErrorText } from "./common";

type Values = Record<string, unknown>;

function text(values: Values, key: string, fallback = ""): string {
  const value = values[key];
  return value === null || value === undefined ? fallback : String(value);
}

function int(value: string): number {
  return /^\d{1,6}$/.test(value.trim()) ? Number(value.trim()) : Number.NaN;
}

/** The form's fields, as text while editing (numbers are checked when saving). */
export interface SettingsForm {
  cooling_off_hours: string;
  phone_change_window_days: string;
  duplicate_window_days: string;
  match_price_pct: string;
  match_quantity_pct: string;
  match_total_pct: string;
  match_total_amount: string;
  require_po: boolean;
  auto_approve_low_risk: boolean;
  auto_approve_limit: string;
  auto_approve_currency: string;
}

/** The form's starting values from the settings in force. Pure. */
export function formOf(settings: PaySettings): SettingsForm {
  const checks = settings.checks as Values;
  const payments = settings.payments as Values;
  return {
    cooling_off_hours: String(settings.cooling_off_hours),
    phone_change_window_days: String(settings.phone_change_window_days),
    duplicate_window_days: text(checks, "duplicate_window_days", "30"),
    match_price_pct: text(checks, "match_price_pct", "0"),
    match_quantity_pct: text(checks, "match_quantity_pct", "0"),
    match_total_pct: text(checks, "match_total_pct", "0"),
    match_total_amount: text(checks, "match_total_amount", "0"),
    require_po: checks.require_po === true,
    auto_approve_low_risk: payments.auto_approve_low_risk === true,
    auto_approve_limit: text(payments, "auto_approve_limit", "0"),
    auto_approve_currency: text(payments, "auto_approve_currency", "AED"),
  };
}

/** What the API gets: only the settings this page shows (the rest keep their values). Pure. */
export function inputOf(form: SettingsForm): PaySettingsInput {
  return {
    cooling_off_hours: int(form.cooling_off_hours),
    phone_change_window_days: int(form.phone_change_window_days),
    checks: {
      duplicate_window_days: int(form.duplicate_window_days),
      match_price_pct: form.match_price_pct.trim(),
      match_quantity_pct: form.match_quantity_pct.trim(),
      match_total_pct: form.match_total_pct.trim(),
      match_total_amount: form.match_total_amount.trim(),
      require_po: form.require_po,
    },
    payments: {
      auto_approve_low_risk: form.auto_approve_low_risk,
      auto_approve_limit: form.auto_approve_limit.trim(),
      auto_approve_currency: form.auto_approve_currency.trim().toUpperCase(),
    },
  };
}

/**
 * Pay settings for admins and finance controllers (`pay.settings.manage`): the
 * cooling-off period of bank-detail changes, the invoice checks (duplicate window,
 * matching tolerances, require a PO) and rule approval of low-risk invoices (off by
 * default, with its limit and currency). Saving needs a second factor; the API checks
 * every value again.
 */
export function PaySettingsView({ initial, totpEnabled }: { initial: PaySettings; totpEnabled: boolean }) {
  const t = useTranslations("Pay.settings");
  const id = useId();
  const [form, setForm] = useState<SettingsForm>(() => formOf(initial));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [stepUp, setStepUp] = useState(false);
  const [saved, setSaved] = useState(false);
  const canManage = initial.can_manage;

  function set<K extends keyof SettingsForm>(key: K, value: SettingsForm[K]) {
    setForm((f) => ({ ...f, [key]: value }));
    setSaved(false);
  }

  async function onSubmit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    setStepUp(false);
    setSaved(false);
    const result = await savePaySettings(inputOf(form));
    setBusy(false);
    if (!result.ok) {
      if (result.code === "second_factor_required") setStepUp(true);
      return setError(result.code);
    }
    setForm(formOf(result.settings));
    setSaved(true);
  }

  const number = (key: keyof SettingsForm, label: string, hint: string, props: React.ComponentProps<"input"> = {}) => (
    <FieldRow label={label} htmlFor={`${id}-${key}`} hint={hint}>
      <Input
        id={`${id}-${key}`}
        name={key}
        inputMode="decimal"
        dir="ltr"
        className="max-w-40"
        value={form[key] as string}
        onChange={(e) => set(key, e.target.value as never)}
        disabled={!canManage || busy}
        required
        {...props}
      />
    </FieldRow>
  );

  return (
    <form onSubmit={onSubmit} className="space-y-6" data-pay-settings aria-label={t("title")}>
      {!canManage && <p className="text-sm text-muted-foreground">{t("readOnly")}</p>}
      <section className="space-y-4 rounded-xl border bg-surface p-5" aria-labelledby={`${id}-bank`}>
        <h2 id={`${id}-bank`} className="font-display text-lg">
          {t("bankTitle")}
        </h2>
        {number("cooling_off_hours", t("coolingOff"), t("coolingOffHint"))}
        {number("phone_change_window_days", t("phoneWindow"), t("phoneWindowHint"))}
      </section>

      <section className="space-y-4 rounded-xl border bg-surface p-5" aria-labelledby={`${id}-checks`}>
        <h2 id={`${id}-checks`} className="font-display text-lg">
          {t("checksTitle")}
        </h2>
        {number("duplicate_window_days", t("duplicateWindow"), t("duplicateWindowHint"))}
        {number("match_price_pct", t("pricePct"), t("pricePctHint"))}
        {number("match_quantity_pct", t("quantityPct"), t("quantityPctHint"))}
        {number("match_total_pct", t("totalPct"), t("totalPctHint"))}
        {number("match_total_amount", t("totalAmount"), t("totalAmountHint"))}
        <label className="flex items-start gap-2 text-sm">
          <Checkbox
            name="require_po"
            checked={form.require_po}
            onChange={(e) => set("require_po", e.target.checked)}
            disabled={!canManage || busy}
            className="mt-0.5"
          />
          <span>
            <span className="block font-medium">{t("requirePo")}</span>
            <span className="block text-xs text-muted-foreground">{t("requirePoHint")}</span>
          </span>
        </label>
      </section>

      <section className="space-y-4 rounded-xl border bg-surface p-5" aria-labelledby={`${id}-rule`}>
        <h2 id={`${id}-rule`} className="font-display text-lg">
          {t("ruleTitle")}
        </h2>
        <label className="flex items-start gap-2 text-sm">
          <Checkbox
            name="auto_approve_low_risk"
            checked={form.auto_approve_low_risk}
            onChange={(e) => set("auto_approve_low_risk", e.target.checked)}
            disabled={!canManage || busy}
            className="mt-0.5"
          />
          <span>
            <span className="block font-medium">{t("ruleOn")}</span>
            <span className="block text-xs text-muted-foreground">{t("ruleOnHint")}</span>
          </span>
        </label>
        {number("auto_approve_limit", t("ruleLimit"), t("ruleLimitHint"))}
        <FieldRow label={t("ruleCurrency")} htmlFor={`${id}-currency`} hint={t("ruleCurrencyHint")}>
          <Input
            id={`${id}-currency`}
            name="auto_approve_currency"
            dir="ltr"
            className="max-w-24 uppercase"
            maxLength={3}
            pattern="[A-Za-z]{3}"
            value={form.auto_approve_currency}
            onChange={(e) => set("auto_approve_currency", e.target.value)}
            disabled={!canManage || busy}
            required
          />
        </FieldRow>
      </section>

      {canManage && (
        <div className="space-y-2">
          <div className="flex items-center gap-3">
            <Button type="submit" disabled={busy}>
              {busy && <Loader2 aria-hidden className="size-4 animate-spin" />}
              {t("save")}
            </Button>
            {saved && (
              <span role="status" className="inline-flex items-center gap-1 text-sm text-muted-foreground">
                <Check aria-hidden className="size-4" />
                {t("saved")}
              </span>
            )}
          </div>
          {stepUp ? (
            <StepUpPrompt totpEnabled={totpEnabled} returnPath="/pay/settings" compact />
          ) : (
            <ErrorText code={error} />
          )}
        </div>
      )}
    </form>
  );
}
