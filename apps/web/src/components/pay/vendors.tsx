// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
"use client";

import { Building2, Loader2, Plus, Search } from "lucide-react";
import { useTranslations } from "next-intl";
import { useId, useMemo, useState } from "react";
import type * as React from "react";

import { EmptyState } from "@/components/states/empty-state";
import { Button } from "@/components/ui/button";
import { Checkbox, Select } from "@/components/ui/form-controls";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Link, useRouter } from "@/i18n/navigation";
import { checkVendor, createVendor, type PayVendorInput } from "@/lib/actions/pay";
import { COUNTRIES, groupIban } from "@/lib/pay/format";
import type { PayVendorCheck, PayVendorSummary } from "@/lib/pay/types";

import { CallbackSourceFields, type CallbackSourceName } from "./callback-source";
import { ErrorText, HoldBadge, VendorStatusBadge } from "./common";

interface FormState {
  legal_name: string;
  legal_name_ar: string;
  trade_name: string;
  country: string;
  trade_licence: string;
  trn: string;
  phone_on_file: string;
  contact_name: string;
  contact_email: string;
  contact_phone: string;
  email_domain: string;
  line1: string;
  city: string;
  iban: string;
  account_name: string;
  bank_name: string;
  swift: string;
  phone_source: CallbackSourceName | "";
  phone_reference: string;
}

const EMPTY: FormState = {
  legal_name: "",
  legal_name_ar: "",
  trade_name: "",
  country: "AE",
  trade_licence: "",
  trn: "",
  phone_on_file: "",
  contact_name: "",
  contact_email: "",
  contact_phone: "",
  email_domain: "",
  line1: "",
  city: "",
  iban: "",
  account_name: "",
  bank_name: "",
  swift: "",
  phone_source: "",
  phone_reference: "",
};

function toInput(f: FormState): PayVendorInput {
  return {
    legal_name: f.legal_name,
    legal_name_ar: f.legal_name_ar,
    trade_name: f.trade_name,
    country: f.country,
    trade_licence: f.trade_licence,
    trn: f.trn,
    phone_on_file: f.phone_on_file,
    address: { line1: f.line1, city: f.city },
    contacts: f.contact_name
      ? [{ name: f.contact_name, email: f.contact_email, phone: f.contact_phone }]
      : [],
    email_domains: f.email_domain ? [f.email_domain] : [],
    ...(f.iban ? { bank: { iban: f.iban, account_name: f.account_name, bank_name: f.bank_name, swift: f.swift } } : {}),
    ...(f.iban && f.phone_source ? { phone_source: { source: f.phone_source, reference: f.phone_reference } } : {}),
  };
}

/** What the check found: IBAN and TRN results and possible duplicates. */
export function CheckResult({ check }: { check: PayVendorCheck }) {
  const t = useTranslations("Pay.form");
  return (
    <div className="space-y-2 rounded-lg border border-border bg-surface-sunken p-4 text-sm" data-vendor-check>
      {check.iban && (
        <p data-iban-check={check.iban.valid ? "valid" : check.iban.error}>
          {check.iban.valid
            ? t("ibanValid", { iban: check.iban.formatted ?? "", bank: check.iban.bank_code ?? "" })
            : t(`ibanError.${check.iban.error}` as "ibanError.checksum")}
        </p>
      )}
      {check.trn && (
        <p data-trn-check={check.trn.valid ? "valid" : check.trn.error}>
          {check.trn.valid ? t("trnValid") : t(`trnError.${check.trn.error}` as "trnError.length")}
        </p>
      )}
      {check.warnings.includes("swift_country_differs") && <p>{t("swiftCountryDiffers")}</p>}
      {check.duplicates.length > 0 ? (
        <div className="space-y-1" data-duplicates>
          <p className="font-medium">{t("duplicates")}</p>
          <ul className="list-disc ps-5">
            {check.duplicates.map((d) => (
              <li key={d.vendor_id}>
                <Link href={`/pay/vendors/${d.vendor_id}`} className="link-inline">
                  <bdi>{d.legal_name}</bdi>
                </Link>{" "}
                (<bdi>{d.reference}</bdi>): {d.reasons.map((r) => t(`duplicateReason.${r}` as "duplicateReason.name")).join(", ")}
              </li>
            ))}
          </ul>
        </div>
      ) : (
        <p data-no-duplicates>{t("noDuplicates")}</p>
      )}
    </div>
  );
}

/** A new vendor, entered by AP. Check first: validation and duplicates, then create. */
export function VendorForm({ onCreated }: { onCreated: (id: string) => void }) {
  const t = useTranslations("Pay.form");
  const ids = useId();
  const [form, setForm] = useState<FormState>(EMPTY);
  const [check, setCheck] = useState<PayVendorCheck | null>(null);
  const [acknowledge, setAcknowledge] = useState(false);
  const [busy, setBusy] = useState<"check" | "create" | null>(null);
  const [error, setError] = useState<string | null>(null);

  function set<K extends keyof FormState>(key: K, value: FormState[K]) {
    setForm((f) => ({ ...f, [key]: value }));
    if (key === "legal_name" || key === "trade_name" || key === "trn" || key === "iban") setCheck(null);
  }

  async function runCheck() {
    setBusy("check");
    setError(null);
    const result = await checkVendor({
      legal_name: form.legal_name,
      trade_name: form.trade_name,
      trn: form.trn,
      iban: form.iban,
      swift: form.swift,
    });
    setBusy(null);
    if (result.ok) setCheck(result.check);
    else setError(result.code);
  }

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy("create");
    setError(null);
    const result = await createVendor(toInput(form), acknowledge);
    setBusy(null);
    if (!result.ok) {
      setError(result.code);
      if (result.code === "vendor_duplicate") void runCheck();
      return;
    }
    onCreated(result.vendor.id);
  }

  const field = (name: keyof FormState) => `${ids}-${name}`;
  const text = (name: keyof FormState, opts: { required?: boolean; max?: number; dir?: "ltr" | "rtl"; type?: string } = {}) => (
    <div className="space-y-1.5">
      <Label htmlFor={field(name)}>{t(`field.${name}` as "field.legal_name")}</Label>
      <Input
        id={field(name)}
        required={opts.required}
        maxLength={opts.max ?? 200}
        type={opts.type ?? "text"}
        dir={opts.dir}
        value={form[name]}
        onChange={(e) => set(name, e.target.value)}
      />
    </div>
  );
  const nameOnly = check !== null && check.duplicates.length > 0 && check.duplicates.every((d) => d.reasons.every((r) => r === "name"));
  return (
    <form onSubmit={submit} className="card-hairline space-y-5 p-5 sm:p-6" data-vendor-form>
      <h2 className="font-display text-xl leading-tight">{t("title")}</h2>
      <p className="text-sm text-muted-foreground">{t("intro")}</p>
      <fieldset className="grid gap-4 sm:grid-cols-2">
        <legend className="eyebrow mb-2">{t("company")}</legend>
        {text("legal_name", { required: true })}
        {text("legal_name_ar", { dir: "rtl" })}
        {text("trade_name")}
        <div className="space-y-1.5">
          <Label htmlFor={field("country")}>{t("field.country")}</Label>
          <Select id={field("country")} value={form.country} onChange={(e) => set("country", e.target.value)}>
            {COUNTRIES.map((c) => (
              <option key={c} value={c}>
                {c}
              </option>
            ))}
          </Select>
        </div>
        {text("trade_licence", { max: 64 })}
        {text("trn", { max: 40, dir: "ltr" })}
        {text("line1")}
        {text("city")}
      </fieldset>
      <fieldset className="grid gap-4 sm:grid-cols-2">
        <legend className="eyebrow mb-2">{t("contact")}</legend>
        {text("phone_on_file", { required: true, max: 40, dir: "ltr", type: "tel" })}
        {text("contact_name", { max: 120 })}
        {text("contact_email", { max: 254, dir: "ltr", type: "email" })}
        {text("contact_phone", { max: 40, dir: "ltr", type: "tel" })}
        {text("email_domain", { max: 253, dir: "ltr" })}
        <p className="text-xs text-muted-foreground sm:col-span-2">{t("phoneHint")}</p>
      </fieldset>
      <fieldset className="grid gap-4 sm:grid-cols-2">
        <legend className="eyebrow mb-2">{t("bank")}</legend>
        {text("iban", { max: 64, dir: "ltr" })}
        {text("account_name")}
        {text("bank_name", { max: 160 })}
        {text("swift", { max: 16, dir: "ltr" })}
        {form.iban && (
          <p className="font-mono text-xs text-muted-foreground sm:col-span-2" dir="ltr">
            {groupIban(form.iban)}
          </p>
        )}
        {form.iban && (
          <div className="space-y-3 sm:col-span-2" data-new-vendor-bank>
            <p className="text-sm text-muted-foreground">{t("bankVerified")}</p>
            <CallbackSourceFields
              withPhone={false}
              value={{ phone: form.phone_on_file, source: form.phone_source, reference: form.phone_reference }}
              onChange={(v) => setForm((f) => ({ ...f, phone_source: v.source, phone_reference: v.reference }))}
            />
          </div>
        )}
      </fieldset>
      {check && <CheckResult check={check} />}
      {nameOnly && (
        <label className="flex items-center gap-2 text-sm">
          <Checkbox checked={acknowledge} onChange={(e) => setAcknowledge(e.target.checked)} data-acknowledge-similar />
          {t("acknowledgeSimilar")}
        </label>
      )}
      <ErrorText code={error} />
      <div className="flex flex-wrap gap-2">
        <Button type="button" variant="outline" disabled={busy !== null || !form.legal_name} onClick={() => void runCheck()} data-vendor-check-button>
          {busy === "check" ? <Loader2 aria-hidden className="animate-spin" /> : <Search aria-hidden />}
          {t("check")}
        </Button>
        <Button type="submit" disabled={busy !== null} data-vendor-create>
          {busy === "create" ? <Loader2 aria-hidden className="animate-spin" /> : <Plus aria-hidden />}
          {t("create")}
        </Button>
      </div>
    </form>
  );
}

type Filter = "all" | "pending" | "hold";

/** The vendor master: search, filter (pending onboardings, payments on hold), add. */
export function VendorsView({ initial, canWrite }: { initial: PayVendorSummary[]; canWrite: boolean }) {
  const t = useTranslations("Pay.vendors");
  const router = useRouter();
  const ids = useId();
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState<Filter>("all");
  const [adding, setAdding] = useState(false);
  const shown = useMemo(() => {
    const needle = query.trim().toLowerCase();
    return initial.filter((v) => {
      if (filter === "pending" && v.status !== "pending") return false;
      if (filter === "hold" && !v.payment_hold) return false;
      if (!needle) return true;
      return [v.legal_name, v.trade_name ?? "", v.reference, v.trn ?? ""].some((s) => s.toLowerCase().includes(needle));
    });
  }, [initial, query, filter]);
  const counts = {
    all: initial.length,
    pending: initial.filter((v) => v.status === "pending").length,
    hold: initial.filter((v) => v.payment_hold).length,
  };
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end gap-3">
        <div className="min-w-56 flex-1 space-y-1.5">
          <Label htmlFor={`${ids}-q`}>{t("search")}</Label>
          <Input id={`${ids}-q`} type="search" value={query} onChange={(e) => setQuery(e.target.value)} />
        </div>
        <div role="tablist" aria-label={t("filter")} className="flex gap-1">
          {(["all", "pending", "hold"] as const).map((f) => (
            <Button
              key={f}
              role="tab"
              aria-selected={filter === f}
              size="sm"
              variant={filter === f ? "default" : "outline"}
              onClick={() => setFilter(f)}
              data-filter={f}
            >
              {t(`filters.${f}`, { count: counts[f] })}
            </Button>
          ))}
        </div>
        {canWrite && !adding && (
          <Button onClick={() => setAdding(true)} data-add-vendor>
            <Plus aria-hidden />
            {t("add")}
          </Button>
        )}
      </div>
      {adding && <VendorForm onCreated={(id) => router.push(`/pay/vendors/${id}`)} />}
      {shown.length === 0 ? (
        <EmptyState icon={<Building2 aria-hidden />} title={t("empty")} body={canWrite ? t("emptyHelp") : undefined} />
      ) : (
        <ul className="card-hairline divide-y divide-border" data-vendor-list>
          {shown.map((v) => (
            <li
              key={v.id}
              className={
                v.payment_hold
                  ? "flex flex-col gap-2 border-s-[3px] border-s-destructive px-4 py-3.5 text-sm sm:flex-row sm:items-center sm:px-5"
                  : "flex flex-col gap-2 border-s-[3px] border-s-transparent px-4 py-3.5 text-sm sm:flex-row sm:items-center sm:px-5"
              }
              data-vendor={v.reference}
            >
              <div className="min-w-0 flex-1 space-y-1">
                <p className="flex flex-wrap items-center gap-2">
                  <Link href={`/pay/vendors/${v.id}`} className="font-medium hover:underline">
                    <bdi>{v.legal_name}</bdi>
                  </Link>
                  <VendorStatusBadge status={v.status} />
                  {v.payment_hold && <HoldBadge reason={v.hold_reason} />}
                </p>
                <p className="text-xs text-muted-foreground">
                  <bdi className="font-mono">{v.reference}</bdi> · {v.country}
                  {v.trn && (
                    <>
                      {" "}
                      · {t("trn")} <bdi className="font-mono">{v.trn}</bdi>
                    </>
                  )}
                  {v.iban_masked && (
                    <>
                      {" "}
                      · <bdi dir="ltr" className="font-mono">{v.iban_masked}</bdi>
                    </>
                  )}
                </p>
              </div>
              {v.open_ticket_id && (
                <Link
                  href={`/pay/bank-changes/${v.open_ticket_id}`}
                  className="inline-flex h-8 shrink-0 items-center rounded-md border border-destructive/45 bg-danger-soft px-3 text-[0.8rem] font-medium text-danger-ink hover:bg-danger-soft/70"
                  data-open-ticket
                >
                  {t("openChange")}
                </Link>
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
