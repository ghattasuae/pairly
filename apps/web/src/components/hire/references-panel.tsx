// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
"use client";

import { AlertTriangle, Loader2, Mail, X } from "lucide-react";
import { useFormatter, useTranslations } from "next-intl";
import { useId, useState } from "react";
import type * as React from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Select } from "@/components/ui/form-controls";
import { Input } from "@/components/ui/input";
import { errorKey } from "@/lib/action-result";
import { askReferee, cancelReference, resendReference, type RefereeInput } from "@/lib/actions/references";
import type { HireReferences } from "@/lib/hire/types";

const RELATIONSHIPS = ["manager", "peer", "direct_report", "client", "other"] as const;
const ANSWER_ORDER = [
  "known_as",
  "company",
  "candidate_title",
  "worked_from",
  "worked_to",
  "strengths",
  "development",
  "reliability",
  "would_rehire",
  "anything_else",
] as const;

/**
 * Reference checks for one candidate (brief 3.8.2): who was asked and where each one
 * stands, the answers, the summary (AI, or from the answers) and the consistency flags
 * against the CV. While the candidate is blind, referees show as "Referee 1". People
 * decide; nothing here moves the candidate.
 */
export function ReferencesPanel({ initial }: { initial: HireReferences }) {
  const t = useTranslations("References");
  const tForm = useTranslations("Referee");
  const tErrors = useTranslations("Errors");
  const format = useFormatter();
  const ids = useId();
  const [data, setData] = useState(initial);
  const [form, setForm] = useState<Omit<RefereeInput, "applicationId">>({ name: "", email: "", relationship: "manager", locale: "en" });
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function run(key: string, action: () => Promise<{ ok: true; references: HireReferences } | { ok: false; code: string }>) {
    setBusy(key);
    setError(null);
    const result = await action();
    setBusy(null);
    if (result.ok) setData(result.references);
    else setError(result.code);
    return result.ok;
  }

  async function ask(e: React.FormEvent) {
    e.preventDefault();
    const ok = await run("ask", () => askReferee({ applicationId: data.application_id, ...form }));
    if (ok) setForm({ name: "", email: "", relationship: "manager", locale: form.locale });
  }

  return (
    <div className="space-y-6" data-references>
      <header className="space-y-1">
        <h1 className="font-display text-2xl">{t("title", { candidate: data.candidate_label })}</h1>
        <p className="text-sm text-muted-foreground">{t("intro")}</p>
        {data.identity_hidden && <p className="text-sm text-muted-foreground">{t("blindNote")}</p>}
      </header>

      {data.summary && (
        <section className="space-y-3 rounded-xl border bg-surface p-5" data-reference-summary>
          <div className="flex flex-wrap items-center justify-between gap-2">
            <h2 className="font-display text-lg">{t("summary")}</h2>
            <span className="text-xs text-muted-foreground">{data.summary.source === "model" ? t("aiNote") : t("answersNote")}</span>
          </div>
          <p>{data.summary.summary}</p>
          {data.summary.strengths.length > 0 && (
            <div>
              <h3 className="text-sm font-medium">{t("strengths")}</h3>
              <ul className="list-disc ps-5 text-sm">
                {data.summary.strengths.map((s) => (
                  <li key={s}>{s}</li>
                ))}
              </ul>
            </div>
          )}
          {data.summary.development.length > 0 && (
            <div>
              <h3 className="text-sm font-medium">{t("development")}</h3>
              <ul className="list-disc ps-5 text-sm">
                {data.summary.development.map((s) => (
                  <li key={s}>{s}</li>
                ))}
              </ul>
            </div>
          )}
          {data.summary.inconsistencies.length > 0 && (
            <div>
              <h3 className="text-sm font-medium">{t("inconsistencies")}</h3>
              <ul className="list-disc ps-5 text-sm">
                {data.summary.inconsistencies.map((s) => (
                  <li key={s}>{s}</li>
                ))}
              </ul>
            </div>
          )}
        </section>
      )}

      {data.flags.length > 0 && (
        <section aria-labelledby={`${ids}-flags`} className="space-y-2 rounded-xl border border-signal/40 bg-signal-soft/40 p-5" data-reference-flags>
          <h2 id={`${ids}-flags`} className="flex items-center gap-2 font-display text-lg">
            <AlertTriangle aria-hidden className="size-4" />
            {t("flags")}
          </h2>
          <p className="text-xs text-muted-foreground">{t("flagsNote")}</p>
          <ul className="space-y-1 text-sm">
            {data.flags.map((f) => (
              <li key={`${f.kind}-${f.referee}-${f.detail}`} data-flag={f.kind}>
                {f.detail}
              </li>
            ))}
          </ul>
        </section>
      )}

      <section aria-labelledby={`${ids}-list`} className="space-y-3">
        <h2 id={`${ids}-list`} className="font-display text-lg">
          {t("referees", { submitted: data.submitted, total: data.items.length })}
        </h2>
        {data.items.length === 0 && <p className="text-sm text-muted-foreground">{t("none")}</p>}
        <ul className="space-y-3">
          {data.items.map((item) => {
            const open = item.status === "requested" || item.status === "in_progress";
            return (
              <li key={item.id} className="space-y-2 rounded-xl border bg-surface p-4 text-sm" data-reference={item.status}>
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <p className="font-medium">
                    {item.label}
                    {item.referee_name && ` · ${item.referee_name}`}
                    <span className="text-muted-foreground"> · {t(`relationship.${item.relationship}` as "relationship.manager")}</span>
                  </p>
                  <Badge variant={item.status === "submitted" ? "default" : "outline"}>{t(`status.${item.status}` as "status.requested")}</Badge>
                </div>
                {item.referee_email && <p className="text-muted-foreground" dir="ltr">{item.referee_email}</p>}
                {item.sent_at && (
                  <p className="text-xs text-muted-foreground">
                    {t("sentAt", { when: format.dateTime(new Date(item.sent_at), { dateStyle: "medium", timeStyle: "short" }) })}
                  </p>
                )}
                {item.answers && (
                  <dl className="grid gap-x-4 gap-y-1 sm:grid-cols-[minmax(0,12rem)_1fr]">
                    {ANSWER_ORDER.filter((k) => item.answers?.[k] !== undefined).map((k) => {
                      const value = item.answers?.[k];
                      const shown =
                        k === "known_as" || k === "would_rehire" ? tForm(`option.${String(value)}` as "option.yes") : String(value);
                      return (
                        <div key={k} className="contents">
                          <dt className="text-muted-foreground">{t(`answer.${k}`)}</dt>
                          <dd>{shown}</dd>
                        </div>
                      );
                    })}
                  </dl>
                )}
                {open && data.can_write && (
                  <div className="flex flex-wrap gap-2">
                    <Button type="button" size="sm" variant="outline" disabled={busy !== null} onClick={() => void run(`send-${item.id}`, () => resendReference(item.id))}>
                      {busy === `send-${item.id}` ? <Loader2 aria-hidden className="animate-spin" /> : <Mail aria-hidden />}
                      {t("resend")}
                    </Button>
                    <Button type="button" size="sm" variant="ghost" disabled={busy !== null} onClick={() => void run(`cancel-${item.id}`, () => cancelReference(item.id))}>
                      <X aria-hidden />
                      {t("cancel")}
                    </Button>
                  </div>
                )}
              </li>
            );
          })}
        </ul>
      </section>

      {data.can_write && (
        <form onSubmit={(e) => void ask(e)} className="space-y-3 rounded-xl border bg-surface p-5" data-ask-referee>
          <h2 className="font-display text-lg">{t("ask")}</h2>
          <p className="text-sm text-muted-foreground">{t("askHint")}</p>
          <div className="grid gap-3 sm:grid-cols-2">
            <div className="space-y-1">
              <label htmlFor={`${ids}-name`} className="text-sm font-medium">
                {t("name")}
              </label>
              <Input id={`${ids}-name`} required minLength={2} maxLength={120} value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />
            </div>
            <div className="space-y-1">
              <label htmlFor={`${ids}-email`} className="text-sm font-medium">
                {t("email")}
              </label>
              <Input id={`${ids}-email`} type="email" dir="ltr" required maxLength={254} value={form.email} onChange={(e) => setForm({ ...form, email: e.target.value })} />
            </div>
            <div className="space-y-1">
              <label htmlFor={`${ids}-rel`} className="text-sm font-medium">
                {t("relationshipLabel")}
              </label>
              <Select
                id={`${ids}-rel`}
                value={form.relationship}
                onChange={(e) => setForm({ ...form, relationship: e.target.value as RefereeInput["relationship"] })}
              >
                {RELATIONSHIPS.map((r) => (
                  <option key={r} value={r}>
                    {t(`relationship.${r}`)}
                  </option>
                ))}
              </Select>
            </div>
            <div className="space-y-1">
              <label htmlFor={`${ids}-locale`} className="text-sm font-medium">
                {t("language")}
              </label>
              <Select id={`${ids}-locale`} value={form.locale} onChange={(e) => setForm({ ...form, locale: e.target.value === "ar" ? "ar" : "en" })}>
                <option value="en">English</option>
                <option value="ar">العربية</option>
              </Select>
            </div>
          </div>
          {error && (
            <p role="alert" className="text-sm text-destructive">
              {tErrors(errorKey(error) as "generic")}
            </p>
          )}
          <Button type="submit" disabled={busy !== null} data-send-request>
            {busy === "ask" && <Loader2 aria-hidden className="animate-spin" />}
            {t("send")}
          </Button>
        </form>
      )}
    </div>
  );
}
