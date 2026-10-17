// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
"use client";

import { Download, Loader2, Plus, Send, Trash2 } from "lucide-react";
import { useFormatter, useLocale, useTranslations } from "next-intl";
import { useId, useState } from "react";
import type * as React from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { errorKey } from "@/lib/action-result";
import { requestChecklist, reviewChecklistItem, saveChecklistConfig } from "@/lib/actions/checklist";
import type { HireChecklist, HireChecklistConfig } from "@/lib/hire/types";

type Doc = { key: string; label: string; label_ar: string };

function keyFor(label: string, taken: Set<string>): string {
  const base =
    label
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, "_")
      .replace(/^_+|_+$/g, "")
      .replace(/^[^a-z]+/, "")
      .slice(0, 28) || "document";
  let key = base;
  for (let n = 2; taken.has(key); n++) key = `${base}_${n}`;
  return key;
}

/** The documents to collect for the job and how long files are kept after hire. */
function ConfigEditor({ initial, onSaved }: { initial: HireChecklistConfig; onSaved: (config: HireChecklistConfig) => void }) {
  const t = useTranslations("Checklist");
  const tErrors = useTranslations("Errors");
  const ids = useId();
  const [docs, setDocs] = useState<Doc[]>(initial.items.map((i) => ({ ...i })));
  const [days, setDays] = useState(String(initial.retention_days));
  const [label, setLabel] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);

  function add(e: React.FormEvent) {
    e.preventDefault();
    const clean = label.trim();
    if (clean.length < 2) return;
    setDocs((list) => [...list, { key: keyFor(clean, new Set(list.map((d) => d.key))), label: clean, label_ar: "" }]);
    setLabel("");
    setSaved(false);
  }

  async function save() {
    setBusy(true);
    setError(null);
    const result = await saveChecklistConfig({ jobId: initial.job_id, items: docs, retentionDays: Number(days) });
    setBusy(false);
    if (!result.ok) return setError(result.code);
    setSaved(true);
    onSaved(result.config);
  }

  return (
    <details className="rounded-xl border bg-surface p-5" data-checklist-config>
      <summary className="cursor-pointer font-medium">{t("configure")}</summary>
      <div className="mt-4 space-y-3">
        <ul className="space-y-2">
          {docs.map((d) => (
            <li key={d.key} className="flex items-center justify-between gap-2 text-sm">
              <span>{d.label}</span>
              <Button
                type="button"
                size="sm"
                variant="ghost"
                aria-label={t("remove", { label: d.label })}
                disabled={docs.length <= 1}
                onClick={() => setDocs((list) => list.filter((x) => x.key !== d.key))}
              >
                <Trash2 aria-hidden />
              </Button>
            </li>
          ))}
        </ul>
        <form onSubmit={add} className="flex flex-wrap gap-2">
          <label htmlFor={`${ids}-new`} className="sr-only">
            {t("newDocument")}
          </label>
          <Input id={`${ids}-new`} className="max-w-xs" placeholder={t("newDocument")} value={label} onChange={(e) => setLabel(e.target.value)} />
          <Button type="submit" size="sm" variant="outline" disabled={docs.length >= 12}>
            <Plus aria-hidden />
            {t("add")}
          </Button>
        </form>
        <div className="space-y-1">
          <label htmlFor={`${ids}-days`} className="text-sm font-medium">
            {t("retention")}
          </label>
          <Input id={`${ids}-days`} type="number" min={1} max={365} dir="ltr" className="max-w-32" value={days} onChange={(e) => setDays(e.target.value)} />
          <p className="text-xs text-muted-foreground">{t("retentionHint")}</p>
        </div>
        {error && (
          <p role="alert" className="text-sm text-destructive">
            {tErrors(errorKey(error) as "generic")}
          </p>
        )}
        {saved && (
          <p role="status" className="text-sm text-muted-foreground">
            {t("savedConfig")}
          </p>
        )}
        <Button type="button" size="sm" disabled={busy} onClick={() => void save()} data-save-checklist-config>
          {busy && <Loader2 aria-hidden className="animate-spin" />}
          {t("saveConfig")}
        </Button>
      </div>
    </details>
  );
}

/**
 * The pre-employment checklist for one candidate (brief 3.8.4), for HR and admins only:
 * which documents were asked for through the portal, which arrived, verify or waive
 * each one, open a file (decrypted on request, audited). Files are kept a short time
 * after hire, then removed.
 */
export function ChecklistPanel({ initial, config }: { initial: HireChecklist; config: HireChecklistConfig }) {
  const t = useTranslations("Checklist");
  const tErrors = useTranslations("Errors");
  const format = useFormatter();
  const locale = useLocale();
  const [data, setData] = useState(initial);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function run(key: string, action: () => Promise<{ ok: true; checklist: HireChecklist } | { ok: false; code: string }>) {
    setBusy(key);
    setError(null);
    const result = await action();
    setBusy(null);
    if (result.ok) setData(result.checklist);
    else setError(result.code);
  }

  return (
    <div className="space-y-6" data-checklist>
      <header className="space-y-1">
        <h1 className="font-display text-2xl">{t("title")}</h1>
        <p className="text-sm text-muted-foreground">{t("intro", { days: data.retention_days })}</p>
      </header>
      {data.items.length === 0 ? (
        <p className="text-sm text-muted-foreground">{t("none")}</p>
      ) : (
        <ul className="space-y-3">
          {data.items.map((item) => (
            <li key={item.id} className="space-y-2 rounded-xl border bg-surface p-4 text-sm" data-checklist-item={item.status}>
              <div className="flex flex-wrap items-center justify-between gap-2">
                <p className="font-medium">{locale === "ar" && item.label_ar ? item.label_ar : item.label}</p>
                <Badge variant={item.status === "verified" ? "default" : "outline"}>{t(`status.${item.status}` as "status.requested")}</Badge>
              </div>
              {item.received_at && (
                <p className="text-muted-foreground">
                  {t("receivedAt", { when: format.dateTime(new Date(item.received_at), { dateStyle: "medium", timeStyle: "short" }) })}
                </p>
              )}
              {item.retention_until && !item.purged_at && (
                <p className="text-muted-foreground">
                  {t("keptUntil", { when: format.dateTime(new Date(item.retention_until), { dateStyle: "medium" }) })}
                </p>
              )}
              {item.purged_at && <p className="text-muted-foreground">{t("purged")}</p>}
              <div className="flex flex-wrap gap-2">
                {item.has_file && (
                  <Button asChild size="sm" variant="outline">
                    <a href={`/api/hire/checklist-items/${item.id}/file`} data-checklist-file>
                      <Download aria-hidden />
                      {t("open")}
                    </a>
                  </Button>
                )}
                {data.can_write && item.status === "received" && (
                  <Button size="sm" disabled={busy !== null} onClick={() => void run(`v-${item.id}`, () => reviewChecklistItem(item.id, "verified"))}>
                    {t("verify")}
                  </Button>
                )}
                {data.can_write && (item.status === "requested" || item.status === "received") && (
                  <Button size="sm" variant="ghost" disabled={busy !== null} onClick={() => void run(`w-${item.id}`, () => reviewChecklistItem(item.id, "waived"))}>
                    {t("waive")}
                  </Button>
                )}
              </div>
            </li>
          ))}
        </ul>
      )}
      {error && (
        <p role="alert" className="text-sm text-destructive">
          {tErrors(errorKey(error) as "generic")}
        </p>
      )}
      {data.can_write && data.missing.length > 0 && (
        <div className="space-y-2 rounded-xl border bg-surface p-5">
          <p className="text-sm">{t("missing", { list: data.missing.map((m) => (locale === "ar" && m.label_ar ? m.label_ar : m.label)).join(", ") })}</p>
          <Button disabled={busy !== null} onClick={() => void run("request", () => requestChecklist(data.application_id))} data-request-checklist>
            {busy === "request" ? <Loader2 aria-hidden className="animate-spin" /> : <Send aria-hidden />}
            {t("request")}
          </Button>
        </div>
      )}
      {data.can_write && (
        <ConfigEditor
          initial={config}
          onSaved={(saved) => {
            const have = new Set(data.items.map((i) => i.key));
            setData({ ...data, retention_days: saved.retention_days, missing: saved.items.filter((i) => !have.has(i.key)) });
          }}
        />
      )}
    </div>
  );
}
