// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
"use client";

import { Download, Loader2, Send } from "lucide-react";
import { useFormatter, useLocale, useTranslations } from "next-intl";
import { useId, useState } from "react";
import type * as React from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Select, Textarea } from "@/components/ui/form-controls";
import { Input } from "@/components/ui/input";
import { errorKey } from "@/lib/action-result";
import { createOffer, offerAction, type OfferInput } from "@/lib/actions/offers";
import type { HireOffer, HireOfferList, HireOfferTemplate } from "@/lib/hire/types";

const MONEY = ["base_monthly", "housing_monthly", "transport_monthly", "other_monthly"] as const;
const OPEN = ["draft", "pending_approval", "changes_requested", "approved", "sending"];

function annual(values: Record<string, string>): number {
  return MONEY.reduce((sum, k) => sum + (Number(values[k]) || 0), 0) * 12;
}

function OfferCard({ offer, onChange }: { offer: HireOffer; onChange: (offer: HireOffer) => void }) {
  const t = useTranslations("Offers");
  const tErrors = useTranslations("Errors");
  const format = useFormatter();
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function act(action: "submit" | "send" | "withdraw") {
    setBusy(action);
    setError(null);
    const result = await offerAction(offer.id, action);
    setBusy(null);
    if (result.ok) onChange(result.offer);
    else setError(result.code);
  }

  const comp = offer.compensation;
  return (
    <li className="space-y-3 rounded-xl border bg-surface p-4 text-sm" data-offer={offer.status}>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="font-medium">
          {offer.reference} · {offer.job_title}
        </p>
        <Badge variant={offer.status === "accepted" ? "default" : "outline"} data-offer-status>
          {offer.sent && offer.status === "sending" ? t("status.sent") : t(`status.${offer.status}` as "status.draft")}
        </Badge>
      </div>
      <p className="text-muted-foreground">
        {t("starts", { date: format.dateTime(new Date(`${offer.start_date}T00:00:00`), { dateStyle: "medium" }) })}
      </p>
      {comp ? (
        <dl className="grid grid-cols-2 gap-x-4 gap-y-1 sm:grid-cols-3" data-compensation>
          {MONEY.filter((k) => Number(comp[k]) > 0).map((k) => (
            <div key={k}>
              <dt className="text-muted-foreground">{t(`field.${k}`)}</dt>
              <dd dir="ltr" className="text-start">
                {comp.currency} {Number(comp[k]).toLocaleString("en", { minimumFractionDigits: 2 })}
              </dd>
            </div>
          ))}
          <div>
            <dt className="text-muted-foreground">{t("field.annual_total")}</dt>
            <dd dir="ltr" className="text-start font-medium">
              {comp.currency} {Number(comp.annual_total).toLocaleString("en", { minimumFractionDigits: 2 })}
            </dd>
          </div>
        </dl>
      ) : (
        <p className="text-muted-foreground" data-compensation-hidden>
          {t("compensationHidden")}
        </p>
      )}
      {offer.conditions.length > 0 && (
        <ul className="list-disc ps-5">
          {offer.conditions.map((c) => (
            <li key={c}>{c}</li>
          ))}
        </ul>
      )}
      {offer.steps.length > 0 && (
        <ol className="flex flex-wrap gap-2" aria-label={t("chain")}>
          {offer.steps.map((s) => (
            <li key={s.name}>
              <Badge variant={s.status === "approved" ? "default" : "outline"}>
                {t(`step.${s.name}` as "step.hr")}: {t(`stepStatus.${s.status}` as "stepStatus.active")}
              </Badge>
            </li>
          ))}
        </ol>
      )}
      {offer.status === "sending" && !offer.sent && <p className="text-muted-foreground">{t("sendingNote")}</p>}
      {error && (
        <p role="alert" className="text-destructive">
          {tErrors(errorKey(error) as "generic")}
        </p>
      )}
      <div className="flex flex-wrap gap-2">
        {comp && (
          <>
            <Button asChild size="sm" variant="outline">
              <a href={`/api/hire/offers/${offer.id}/pdf?locale=en`} data-offer-pdf="en">
                <Download aria-hidden />
                {t("pdfEn")}
              </a>
            </Button>
            <Button asChild size="sm" variant="outline">
              <a href={`/api/hire/offers/${offer.id}/pdf?locale=ar`} data-offer-pdf="ar">
                <Download aria-hidden />
                {t("pdfAr")}
              </a>
            </Button>
          </>
        )}
        {offer.can_edit && (
          <Button size="sm" disabled={busy !== null} onClick={() => void act("submit")} data-submit-offer>
            {busy === "submit" && <Loader2 aria-hidden className="animate-spin" />}
            {t("submit")}
          </Button>
        )}
        {offer.can_send && (
          <Button size="sm" disabled={busy !== null} onClick={() => void act("send")} data-send-offer>
            {busy === "send" ? <Loader2 aria-hidden className="animate-spin" /> : <Send aria-hidden />}
            {t("send")}
          </Button>
        )}
        {offer.can_withdraw && (
          <Button size="sm" variant="ghost" disabled={busy !== null} onClick={() => void act("withdraw")}>
            {t("withdraw")}
          </Button>
        )}
      </div>
    </li>
  );
}

/**
 * The offer builder for one candidate (brief 3.8.3): offers so far with their approval
 * chain, and a new offer from a template. Compensation shows only to people allowed to
 * see it. An offer reaches the candidate only after its approvals; there is no
 * e-signature: the candidate accepts or declines in the portal.
 */
export function OfferBuilder({
  initial,
  templates,
  defaultTitle,
}: {
  initial: HireOfferList;
  templates: HireOfferTemplate[];
  defaultTitle: string;
}) {
  const t = useTranslations("Offers");
  const tErrors = useTranslations("Errors");
  const locale = useLocale() === "ar" ? "ar" : "en";
  const ids = useId();
  const [items, setItems] = useState(initial.items);
  const first = templates[0];
  const [template, setTemplate] = useState<"standard" | "fixed_term">((first?.key as "standard") ?? "standard");
  const chosen = templates.find((x) => x.key === template) ?? first;
  const [offerLocale, setOfferLocale] = useState<"en" | "ar">(locale);
  const [startDate, setStartDate] = useState("");
  const [jobTitle, setJobTitle] = useState(defaultTitle);
  const [money, setMoney] = useState<Record<string, string>>({ base_monthly: "", housing_monthly: "0", transport_monthly: "0", other_monthly: "0" });
  const [bonus, setBonus] = useState("0");
  const [currency, setCurrency] = useState("AED");
  const [conditions, setConditions] = useState((chosen?.conditions ?? []).join("\n"));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const open = items.some((o) => OPEN.includes(o.status));

  function pickTemplate(key: string, lang: "en" | "ar") {
    const next = templates.find((x) => x.key === key);
    if (next) setConditions((lang === "ar" ? next.conditions_ar : next.conditions).join("\n"));
  }

  async function create(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    const input: OfferInput = {
      applicationId: initial.application_id,
      template,
      startDate,
      locale: offerLocale,
      jobTitle,
      conditions: conditions
        .split("\n")
        .map((c) => c.trim())
        .filter(Boolean),
      compensation: {
        currency,
        base_monthly: money.base_monthly || "0",
        housing_monthly: money.housing_monthly || "0",
        transport_monthly: money.transport_monthly || "0",
        other_monthly: money.other_monthly || "0",
        bonus_target_pct: bonus || "0",
      },
    };
    const result = await createOffer(input);
    setBusy(false);
    if (result.ok) setItems((list) => [result.offer, ...list]);
    else setError(result.code);
  }

  return (
    <div className="space-y-6" data-offer-builder>
      <header className="space-y-1">
        <h1 className="font-display text-2xl">{t("title")}</h1>
        <p className="text-sm text-muted-foreground">{t("intro")}</p>
      </header>
      {items.length === 0 ? (
        <p className="text-sm text-muted-foreground">{t("none")}</p>
      ) : (
        <ul className="space-y-3">
          {items.map((offer) => (
            <OfferCard key={offer.id} offer={offer} onChange={(next) => setItems((list) => list.map((o) => (o.id === next.id ? next : o)))} />
          ))}
        </ul>
      )}
      {initial.can_write && !open && (
        <form onSubmit={(e) => void create(e)} className="space-y-4 rounded-xl border bg-surface p-5" data-new-offer>
          <h2 className="font-display text-lg">{t("new")}</h2>
          <div className="grid gap-3 sm:grid-cols-2">
            <div className="space-y-1">
              <label htmlFor={`${ids}-template`} className="text-sm font-medium">
                {t("template")}
              </label>
              <Select
                id={`${ids}-template`}
                value={template}
                onChange={(e) => {
                  setTemplate(e.target.value === "fixed_term" ? "fixed_term" : "standard");
                  pickTemplate(e.target.value, offerLocale);
                }}
              >
                {templates.map((x) => (
                  <option key={x.key} value={x.key}>
                    {locale === "ar" ? x.name_ar : x.name}
                  </option>
                ))}
              </Select>
            </div>
            <div className="space-y-1">
              <label htmlFor={`${ids}-locale`} className="text-sm font-medium">
                {t("language")}
              </label>
              <Select
                id={`${ids}-locale`}
                value={offerLocale}
                onChange={(e) => {
                  const lang = e.target.value === "ar" ? "ar" : "en";
                  setOfferLocale(lang);
                  pickTemplate(template, lang);
                }}
              >
                <option value="en">English</option>
                <option value="ar">العربية</option>
              </Select>
            </div>
            <div className="space-y-1">
              <label htmlFor={`${ids}-title`} className="text-sm font-medium">
                {t("jobTitle")}
              </label>
              <Input id={`${ids}-title`} maxLength={160} value={jobTitle} onChange={(e) => setJobTitle(e.target.value)} />
            </div>
            <div className="space-y-1">
              <label htmlFor={`${ids}-start`} className="text-sm font-medium">
                {t("startDate")}
              </label>
              <Input id={`${ids}-start`} type="date" required dir="ltr" value={startDate} onChange={(e) => setStartDate(e.target.value)} />
            </div>
          </div>
          <fieldset className="space-y-2">
            <legend className="text-sm font-medium">{t("compensation")}</legend>
            <div className="grid gap-3 sm:grid-cols-3">
              <div className="space-y-1">
                <label htmlFor={`${ids}-currency`} className="text-xs text-muted-foreground">
                  {t("field.currency")}
                </label>
                <Input
                  id={`${ids}-currency`}
                  dir="ltr"
                  maxLength={3}
                  value={currency}
                  onChange={(e) => setCurrency(e.target.value.toUpperCase())}
                />
              </div>
              {MONEY.map((k) => (
                <div key={k} className="space-y-1">
                  <label htmlFor={`${ids}-${k}`} className="text-xs text-muted-foreground">
                    {t(`field.${k}`)}
                  </label>
                  <Input
                    id={`${ids}-${k}`}
                    inputMode="decimal"
                    dir="ltr"
                    required={k === "base_monthly"}
                    value={money[k]}
                    onChange={(e) => setMoney((m) => ({ ...m, [k]: e.target.value }))}
                  />
                </div>
              ))}
              <div className="space-y-1">
                <label htmlFor={`${ids}-bonus`} className="text-xs text-muted-foreground">
                  {t("field.bonus_target_pct")}
                </label>
                <Input id={`${ids}-bonus`} inputMode="decimal" dir="ltr" value={bonus} onChange={(e) => setBonus(e.target.value)} />
              </div>
            </div>
            <p className="text-sm" data-annual-total>
              {t("annualTotal", { amount: `${currency} ${annual(money).toLocaleString("en", { minimumFractionDigits: 2 })}` })}
            </p>
          </fieldset>
          <div className="space-y-1">
            <label htmlFor={`${ids}-conditions`} className="text-sm font-medium">
              {t("conditions")}
            </label>
            <Textarea id={`${ids}-conditions`} rows={5} value={conditions} onChange={(e) => setConditions(e.target.value)} />
            <p className="text-xs text-muted-foreground">{t("conditionsHint")}</p>
          </div>
          {error && (
            <p role="alert" className="text-sm text-destructive">
              {tErrors(errorKey(error) as "generic")}
            </p>
          )}
          <Button type="submit" disabled={busy || !startDate || !money.base_monthly} data-create-offer>
            {busy && <Loader2 aria-hidden className="animate-spin" />}
            {t("create")}
          </Button>
        </form>
      )}
    </div>
  );
}
