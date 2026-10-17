// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
"use client";

import { Briefcase, Loader2, Plus } from "lucide-react";
import { useFormatter, useLocale, useTranslations } from "next-intl";
import { useId, useState } from "react";
import type * as React from "react";

import { EmptyState } from "@/components/states/empty-state";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Select, Textarea } from "@/components/ui/form-controls";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Link, useRouter } from "@/i18n/navigation";
import { errorKey } from "@/lib/action-result";
import {
  cancelRequisition,
  createRequisition,
  openJobFromRequisition,
  type RequisitionFormInput,
} from "@/lib/actions/requisitions";
import { actionKey } from "@/lib/hire/board";
import type { HireRequisition } from "@/lib/hire/types";

const CURRENCIES = ["AED", "SAR", "QAR", "KWD", "BHD", "OMR", "USD", "EUR", "GBP"] as const;
const WORK_MODELS = ["onsite", "hybrid", "remote"] as const;

function emptyForm(): RequisitionFormInput {
  return {
    title: "",
    title_ar: "",
    team: "",
    budget: "",
    currency: "AED",
    headcount: 1,
    location: "",
    work_model: "onsite",
    start_date: "",
    justification: "",
  };
}

/** The request form (brief 3.1.1): one screen, every field the approvers need. */
export function RequisitionForm({ onCreated }: { onCreated: (r: HireRequisition) => void }) {
  const t = useTranslations("Requisitions");
  const tErrors = useTranslations("Errors");
  const ids = useId();
  const [form, setForm] = useState<RequisitionFormInput>(emptyForm);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  function set<K extends keyof RequisitionFormInput>(k: K, v: RequisitionFormInput[K]) {
    setForm((f) => ({ ...f, [k]: v }));
  }

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    const result = await createRequisition(form, actionKey());
    setBusy(false);
    if (!result.ok) {
      setError(result.code);
      return;
    }
    setForm(emptyForm());
    onCreated(result.requisition);
  }

  const field = (name: keyof RequisitionFormInput) => `${ids}-${name}`;
  return (
    <form onSubmit={submit} className="space-y-4 rounded-xl border bg-surface p-5" data-requisition-form>
      <h2 className="font-display text-xl">{t("form.title")}</h2>
      <p className="text-sm text-muted-foreground">{t("form.intro")}</p>
      <div className="grid gap-4 sm:grid-cols-2">
        <div className="space-y-1.5">
          <Label htmlFor={field("title")}>{t("form.jobTitle")}</Label>
          <Input id={field("title")} required maxLength={160} value={form.title} onChange={(e) => set("title", e.target.value)} />
        </div>
        <div className="space-y-1.5">
          <Label htmlFor={field("title_ar")}>{t("form.jobTitleAr")}</Label>
          <Input
            id={field("title_ar")}
            dir="rtl"
            lang="ar"
            maxLength={160}
            value={form.title_ar}
            onChange={(e) => set("title_ar", e.target.value)}
          />
        </div>
        <div className="space-y-1.5">
          <Label htmlFor={field("team")}>{t("form.team")}</Label>
          <Input id={field("team")} required maxLength={120} value={form.team} onChange={(e) => set("team", e.target.value)} />
        </div>
        <div className="space-y-1.5">
          <Label htmlFor={field("location")}>{t("form.location")}</Label>
          <Input
            id={field("location")}
            required
            maxLength={160}
            value={form.location}
            onChange={(e) => set("location", e.target.value)}
          />
        </div>
        <div className="space-y-1.5">
          <Label htmlFor={field("budget")}>{t("form.budget")}</Label>
          <div className="flex gap-2">
            <Input
              id={field("budget")}
              required
              inputMode="decimal"
              pattern="\d{1,12}(\.\d{1,2})?"
              value={form.budget}
              onChange={(e) => set("budget", e.target.value.trim())}
            />
            <Select
              aria-label={t("form.currency")}
              className="w-28"
              value={form.currency}
              onChange={(e) => set("currency", e.target.value)}
            >
              {CURRENCIES.map((c) => (
                <option key={c} value={c}>
                  {c}
                </option>
              ))}
            </Select>
          </div>
          <p className="text-xs text-muted-foreground">{t("form.budgetHint")}</p>
        </div>
        <div className="space-y-1.5">
          <Label htmlFor={field("headcount")}>{t("form.headcount")}</Label>
          <Input
            id={field("headcount")}
            type="number"
            min={1}
            max={100}
            required
            value={form.headcount}
            onChange={(e) => set("headcount", Number(e.target.value))}
          />
        </div>
        <div className="space-y-1.5">
          <Label htmlFor={field("work_model")}>{t("form.workModel")}</Label>
          <Select
            id={field("work_model")}
            value={form.work_model}
            onChange={(e) => set("work_model", e.target.value as RequisitionFormInput["work_model"])}
          >
            {WORK_MODELS.map((m) => (
              <option key={m} value={m}>
                {t(`workModel.${m}`)}
              </option>
            ))}
          </Select>
        </div>
        <div className="space-y-1.5">
          <Label htmlFor={field("start_date")}>{t("form.startDate")}</Label>
          <Input
            id={field("start_date")}
            type="date"
            required
            value={form.start_date}
            onChange={(e) => set("start_date", e.target.value)}
          />
        </div>
      </div>
      <div className="space-y-1.5">
        <Label htmlFor={field("justification")}>{t("form.justification")}</Label>
        <Textarea
          id={field("justification")}
          maxLength={1000}
          value={form.justification}
          onChange={(e) => set("justification", e.target.value)}
        />
      </div>
      {error && (
        <p role="alert" className="text-sm text-destructive">
          {tErrors(errorKey(error) as "generic")}
        </p>
      )}
      <Button type="submit" disabled={busy} data-requisition-submit>
        {busy ? <Loader2 aria-hidden className="animate-spin" /> : <Plus aria-hidden />}
        {t("form.submit")}
      </Button>
    </form>
  );
}

function statusVariant(status: string): "default" | "outline" | "signal" {
  if (status === "approved") return "default";
  if (status === "pending_approval") return "signal";
  return "outline";
}

/** Every requisition with its approval progress and the next action, newest first. */
export function RequisitionList({ items, onChange }: { items: HireRequisition[]; onChange: (r: HireRequisition) => void }) {
  const t = useTranslations("Requisitions");
  const tErrors = useTranslations("Errors");
  const format = useFormatter();
  const locale = useLocale();
  const router = useRouter();
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function cancel(r: HireRequisition) {
    setBusy(r.id);
    const result = await cancelRequisition(r.id);
    setBusy(null);
    if (result.ok) onChange(result.requisition);
    else setError(result.code);
  }

  async function openJob(r: HireRequisition) {
    setBusy(r.id);
    const result = await openJobFromRequisition(r.id, actionKey());
    setBusy(null);
    if (result.ok) router.push(`/hire/jobs/${result.job.id}`);
    else setError(result.code);
  }

  if (items.length === 0) return <EmptyState icon={<Briefcase aria-hidden />} title={t("list.empty")} />;
  return (
    <div className="space-y-2">
      {error && (
        <p role="alert" className="text-sm text-destructive">
          {tErrors(errorKey(error) as "generic")}
        </p>
      )}
      <ul className="divide-y rounded-xl border bg-surface" data-requisition-list>
        {items.map((r) => (
          <li key={r.id} className="flex flex-col gap-3 px-4 py-3 text-sm sm:flex-row sm:items-center" data-requisition={r.reference}>
            <div className="min-w-0 flex-1 space-y-1">
              <p className="flex flex-wrap items-center gap-2">
                <span className="font-medium">{locale === "ar" && r.title_ar ? r.title_ar : r.title}</span>
                <Badge variant={statusVariant(r.status)} data-status={r.status}>
                  {t(`status.${r.status}` as "status.approved")}
                </Badge>
              </p>
              <p className="text-xs text-muted-foreground">
                <bdi>{r.reference}</bdi> · {r.team} · {r.location} · {t(`workModel.${r.work_model}` as "workModel.onsite")} ·{" "}
                {t("list.headcount", { count: r.headcount })} ·{" "}
                {format.number(Number(r.budget), { style: "currency", currency: r.currency, maximumFractionDigits: 0 })} ·{" "}
                {t("list.start", { date: format.dateTime(new Date(`${r.start_date}T00:00:00`), { dateStyle: "medium" }) })}
              </p>
              {(r.steps ?? []).length > 0 && (
                <p className="text-xs text-muted-foreground" data-steps>
                  {(r.steps ?? []).map((s) => `${t(`step.${s.name}` as "step.hr")}: ${t(`stepStatus.${s.status}` as "stepStatus.active")}`).join(" · ")}
                </p>
              )}
            </div>
            <div className="flex shrink-0 flex-wrap gap-2">
              {r.status === "pending_approval" && !r.mine && r.approval_id && (
                <Link href={`/inbox/${r.approval_id}`} className="inline-flex h-8 items-center rounded-md border px-3 text-[0.8rem] hover:bg-accent">
                  {t("list.review")}
                </Link>
              )}
              {r.can_cancel && (
                <Button size="sm" variant="outline" disabled={busy === r.id} onClick={() => void cancel(r)}>
                  {t("list.withdraw")}
                </Button>
              )}
              {r.can_open_job && (
                <Button size="sm" disabled={busy === r.id} onClick={() => void openJob(r)} data-open-job>
                  {t("list.openJob")}
                </Button>
              )}
              {r.job_id && (
                <Link href={`/hire/jobs/${r.job_id}`} className="inline-flex h-8 items-center rounded-md border px-3 text-[0.8rem] hover:bg-accent">
                  {t("list.viewJob")}
                </Link>
              )}
            </div>
          </li>
        ))}
      </ul>
    </div>
  );
}

/** The requisitions page body: the form for people who may request, then the list. */
export function RequisitionsView({ initial, canCreate }: { initial: HireRequisition[]; canCreate: boolean }) {
  const [items, setItems] = useState(initial);
  const upsert = (r: HireRequisition) =>
    setItems((list) => (list.some((i) => i.id === r.id) ? list.map((i) => (i.id === r.id ? r : i)) : [r, ...list]));
  return (
    <div className="space-y-6">
      {canCreate && <RequisitionForm onCreated={upsert} />}
      <RequisitionList items={items} onChange={upsert} />
    </div>
  );
}
