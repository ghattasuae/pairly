// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
"use client";

import { Check, FileUp, Loader2 } from "lucide-react";
import { useTranslations } from "next-intl";
import { useEffect, useId, useRef, useState } from "react";
import type * as React from "react";

import { Button } from "@/components/ui/button";
import { Checkbox, Select } from "@/components/ui/form-controls";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { loadVendorForm, openVendorLink, submitVendorForm, type VendorOnboardingForm } from "@/lib/actions/vendor-onboarding";
import { COUNTRIES, groupIban } from "@/lib/pay/format";
import type { PayOnboardingPage } from "@/lib/pay/types";
import { UPLOAD_HEADER } from "@/lib/upload-guard";

import { ErrorText } from "./common";

const EMPTY: VendorOnboardingForm = {
  legal_name: "",
  legal_name_ar: "",
  trade_name: "",
  country: "AE",
  trade_licence: "",
  trn: "",
  address: { line1: "", line2: "", city: "", region: "", po_box: "" },
  contact: { name: "", role: "", email: "", phone: "" },
  phone_on_file: "",
  email_domain: "",
  bank: { iban: "", account_name: "", bank_name: "", swift: "" },
};

export interface OnboardingActions {
  submit: typeof submitVendorForm;
  upload: (file: File) => Promise<{ ok: true; page: PayOnboardingPage } | { ok: false; code: string }>;
}

async function uploadDocument(file: File): Promise<{ ok: true; page: PayOnboardingPage } | { ok: false; code: string }> {
  const body = new FormData();
  body.set("file", file);
  try {
    const response = await fetch("/api/vendor-onboarding/documents", {
      method: "POST",
      body,
      headers: { [UPLOAD_HEADER]: "1" },
      credentials: "same-origin",
    });
    if (response.ok) return { ok: true, page: (await response.json()) as PayOnboardingPage };
    const problem = (await response.json().catch(() => ({}))) as { code?: string };
    return { ok: false, code: problem.code ?? "unavailable" };
  } catch {
    return { ok: false, code: "unavailable" };
  }
}

const DEFAULT_ACTIONS: OnboardingActions = { submit: submitVendorForm, upload: uploadDocument };

/** The vendor's own form: company, contact, bank details and documents, then submit. */
export function VendorOnboardingForm({
  initial,
  actions = DEFAULT_ACTIONS,
}: {
  initial: PayOnboardingPage;
  actions?: OnboardingActions;
}) {
  const t = useTranslations("VendorOnboarding");
  const ids = useId();
  const [page, setPage] = useState(initial);
  const [form, setForm] = useState<VendorOnboardingForm>({ ...EMPTY, legal_name: initial.company_hint ?? "" });
  const [confirm, setConfirm] = useState(false);
  const [busy, setBusy] = useState<"upload" | "submit" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState<string | null>(null);

  function set<K extends keyof VendorOnboardingForm>(key: K, value: VendorOnboardingForm[K]) {
    setForm((f) => ({ ...f, [key]: value }));
  }

  async function upload(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    e.target.value = "";
    if (!file) return;
    setBusy("upload");
    setError(null);
    const result = await actions.upload(file);
    setBusy(null);
    if (result.ok) setPage(result.page);
    else setError(result.code);
  }

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy("submit");
    setError(null);
    const result = await actions.submit(form);
    setBusy(null);
    if (result.ok) setDone(result.reference);
    else setError(result.code);
  }

  if (done) {
    return (
      <div role="status" className="space-y-2 rounded-xl border bg-surface p-6" data-onboarding-done>
        <p className="flex items-center gap-2 font-display text-2xl">
          <Check aria-hidden className="size-6 text-primary" />
          {t("doneTitle")}
        </p>
        <p>{t("doneBody", { company: page.company, reference: done })}</p>
      </div>
    );
  }

  const id = (name: string) => `${ids}-${name}`;
  const line = (label: string, name: string, value: string, onChange: (v: string) => void, opts: { required?: boolean; max?: number; dir?: "ltr" | "rtl"; type?: string } = {}) => (
    <div className="space-y-1.5">
      <Label htmlFor={id(name)}>
        {label}
        {opts.required && <span aria-hidden> *</span>}
      </Label>
      <Input
        id={id(name)}
        required={opts.required}
        maxLength={opts.max ?? 200}
        dir={opts.dir}
        type={opts.type ?? "text"}
        value={value}
        onChange={(e) => onChange(e.target.value)}
      />
    </div>
  );

  return (
    <form onSubmit={submit} className="space-y-6" data-onboarding-form>
      <div className="space-y-2">
        <h1 className="font-display text-3xl">{t("heading", { company: page.company })}</h1>
        <p className="text-muted-foreground">{t("intro")}</p>
      </div>
      <fieldset className="grid gap-4 rounded-xl border bg-surface p-5 sm:grid-cols-2">
        <legend className="px-1 font-medium">{t("section.company")}</legend>
        {line(t("field.legal_name"), "legal_name", form.legal_name, (v) => set("legal_name", v), { required: true })}
        {line(t("field.legal_name_ar"), "legal_name_ar", form.legal_name_ar, (v) => set("legal_name_ar", v), { dir: "rtl" })}
        {line(t("field.trade_name"), "trade_name", form.trade_name, (v) => set("trade_name", v))}
        <div className="space-y-1.5">
          <Label htmlFor={id("country")}>{t("field.country")}</Label>
          <Select id={id("country")} value={form.country} onChange={(e) => set("country", e.target.value)}>
            {COUNTRIES.map((c) => (
              <option key={c} value={c}>
                {c}
              </option>
            ))}
          </Select>
        </div>
        {line(t("field.trade_licence"), "trade_licence", form.trade_licence, (v) => set("trade_licence", v), { max: 64 })}
        {line(t("field.trn"), "trn", form.trn, (v) => set("trn", v), { max: 40, dir: "ltr" })}
        {line(t("field.line1"), "line1", form.address.line1, (v) => set("address", { ...form.address, line1: v }))}
        {line(t("field.city"), "city", form.address.city, (v) => set("address", { ...form.address, city: v }))}
      </fieldset>
      <fieldset className="grid gap-4 rounded-xl border bg-surface p-5 sm:grid-cols-2">
        <legend className="px-1 font-medium">{t("section.contact")}</legend>
        {line(t("field.phone_on_file"), "phone_on_file", form.phone_on_file, (v) => set("phone_on_file", v), { required: true, max: 40, dir: "ltr", type: "tel" })}
        {line(t("field.contact_name"), "contact_name", form.contact.name, (v) => set("contact", { ...form.contact, name: v }), { required: true, max: 120 })}
        {line(t("field.contact_role"), "contact_role", form.contact.role, (v) => set("contact", { ...form.contact, role: v }), { max: 80 })}
        {line(t("field.contact_email"), "contact_email", form.contact.email, (v) => set("contact", { ...form.contact, email: v }), { max: 254, dir: "ltr", type: "email" })}
        {line(t("field.email_domain"), "email_domain", form.email_domain, (v) => set("email_domain", v), { max: 253, dir: "ltr" })}
        <p className="text-xs text-muted-foreground sm:col-span-2">{t("phoneHint")}</p>
      </fieldset>
      <fieldset className="grid gap-4 rounded-xl border bg-surface p-5 sm:grid-cols-2">
        <legend className="px-1 font-medium">{t("section.bank")}</legend>
        {line(t("field.iban"), "iban", form.bank.iban, (v) => set("bank", { ...form.bank, iban: v }), { required: true, max: 64, dir: "ltr" })}
        {line(t("field.account_name"), "account_name", form.bank.account_name, (v) => set("bank", { ...form.bank, account_name: v }), { required: true })}
        {line(t("field.bank_name"), "bank_name", form.bank.bank_name, (v) => set("bank", { ...form.bank, bank_name: v }), { max: 160 })}
        {line(t("field.swift"), "swift", form.bank.swift, (v) => set("bank", { ...form.bank, swift: v }), { max: 16, dir: "ltr" })}
        {form.bank.iban && (
          <p className="font-mono text-xs text-muted-foreground sm:col-span-2" dir="ltr">
            {groupIban(form.bank.iban)}
          </p>
        )}
        <p className="text-xs text-muted-foreground sm:col-span-2">{t("bankHint")}</p>
      </fieldset>
      <fieldset className="space-y-3 rounded-xl border bg-surface p-5">
        <legend className="px-1 font-medium">{t("section.documents")}</legend>
        <p className="text-sm text-muted-foreground">{t("documentsHint", { max: page.max_documents })}</p>
        {page.documents.length > 0 && (
          <ul className="space-y-1 text-sm" data-documents>
            {page.documents.map((d) => (
              <li key={d.id} className="flex items-center gap-2">
                <Check aria-hidden className="size-4 text-primary" />
                <bdi>{d.filename ?? t("document")}</bdi>
              </li>
            ))}
          </ul>
        )}
        {page.documents.length < page.max_documents && (
          <label className="inline-flex cursor-pointer items-center gap-2 rounded-md border px-3 py-2 text-sm hover:bg-accent">
            {busy === "upload" ? <Loader2 aria-hidden className="size-4 animate-spin" /> : <FileUp aria-hidden className="size-4" />}
            {t("addDocument")}
            <input type="file" className="sr-only" accept=".pdf,.png,.jpg,.jpeg,.docx" onChange={(e) => void upload(e)} data-document-input />
          </label>
        )}
      </fieldset>
      <label className="flex items-start gap-2 text-sm">
        <Checkbox checked={confirm} onChange={(e) => setConfirm(e.target.checked)} required data-confirm />
        {t("confirm")}
      </label>
      <ErrorText code={error} />
      <Button type="submit" size="lg" disabled={busy !== null || !confirm} data-onboarding-submit>
        {busy === "submit" && <Loader2 aria-hidden className="animate-spin" />}
        {t("submit")}
      </Button>
    </form>
  );
}

/**
 * `/<locale>/vendor-onboarding#<link>`: the vendor's signed invite link. The link stays
 * in the fragment (never sent to a server log); the page posts it once, the session goes
 * into an HTTP-only cookie, and the form opens.
 */
export function VendorOnboardingApp() {
  const t = useTranslations("VendorOnboarding");
  const [state, setState] = useState<{ kind: "loading" } | { kind: "page"; page: PayOnboardingPage } | { kind: "error"; code: string }>({
    kind: "loading",
  });
  const started = useRef(false);

  useEffect(() => {
    if (started.current) return;
    started.current = true;
    const token = window.location.hash.replace(/^#/, "");
    if (token) window.history.replaceState(null, "", window.location.pathname);
    void (async () => {
      if (token.startsWith("vi1.")) {
        const opened = await openVendorLink(token);
        if (!opened.ok) {
          const existing = await loadVendorForm();
          setState(existing.ok ? { kind: "page", page: existing.page } : { kind: "error", code: "link" });
          return;
        }
      }
      const loaded = await loadVendorForm();
      setState(loaded.ok ? { kind: "page", page: loaded.page } : { kind: "error", code: "link" });
    })();
  }, []);

  if (state.kind === "loading") {
    return (
      <p role="status" className="flex items-center justify-center gap-2 py-16 text-muted-foreground">
        <Loader2 className="size-4 animate-spin" aria-hidden />
        {t("opening")}
      </p>
    );
  }
  if (state.kind === "error") {
    return (
      <div role="alert" className="space-y-2 rounded-xl border bg-surface p-5" data-link-invalid>
        <p className="font-medium">{t("linkInvalid")}</p>
        <p className="text-sm text-muted-foreground">{t("linkHelp")}</p>
      </div>
    );
  }
  return <VendorOnboardingForm initial={state.page} />;
}
