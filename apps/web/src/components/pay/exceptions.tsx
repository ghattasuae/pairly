// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
"use client";

import { AlarmClock, CheckCircle2, Inbox, Loader2, Mail, Send } from "lucide-react";
import { useTranslations } from "next-intl";
import { useEffect, useId, useRef, useState } from "react";

import { EmptyState } from "@/components/states/empty-state";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/form-controls";
import { Label } from "@/components/ui/label";
import { Link, useRouter } from "@/i18n/navigation";
import { draftExceptionMessage, sendExceptionMessage } from "@/lib/actions/pay";
import {
  EXCEPTION_CATEGORIES,
  ageParts,
  itemsOf,
  move,
  typingIn,
  type ExceptionCategoryKey,
} from "@/lib/pay/exceptions";
import type { PayExceptionItem, PayExceptions, PayMessage, PayMessageDraft } from "@/lib/pay/types";
import { cn } from "@/lib/utils";

import { ErrorText } from "./common";

type Lang = "en" | "ar";

function Age({ hours, overdue }: { hours: number; overdue: boolean }) {
  const t = useTranslations("Pay.exceptions");
  const parts = ageParts(hours);
  return (
    <span className="inline-flex items-center gap-1 text-xs tabular-nums" data-age={hours}>
      <AlarmClock aria-hidden className="size-3" />
      {parts.days > 0 ? t("ageDays", parts) : t("ageHours", { hours: parts.hours })}
      {overdue && (
        <Badge variant="signal" data-overdue>
          {t("overdue")}
        </Badge>
      )}
    </span>
  );
}

/** Draft a message from a template, edit it, and send it for approval. */
function Composer({ item, onClose }: { item: PayExceptionItem; onClose: () => void }) {
  const t = useTranslations("Pay.exceptions");
  const id = useId();
  const options = item.categories.flatMap((c) => c.templates.map((tpl) => ({ category: c.category, ...tpl })));
  const [choice, setChoice] = useState(options[0] ? `${options[0].category}:${options[0].id}` : "");
  const [lang, setLang] = useState<Lang>("en");
  const [personalise, setPersonalise] = useState(false);
  const [draft, setDraft] = useState<PayMessageDraft | null>(null);
  const [text, setText] = useState("");
  const [recipient, setRecipient] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [sent, setSent] = useState<PayMessage | null>(null);
  const [category, template] = choice.split(":");

  async function makeDraft() {
    if (!category || !template) return;
    setBusy(true);
    setError(null);
    const result = await draftExceptionMessage(item.id, { category, template_id: template, locale: lang, personalise });
    setBusy(false);
    if (!result.ok) return setError(result.code);
    setDraft(result.draft);
    setText(result.draft.message);
    setRecipient(result.draft.contacts[0]?.email ?? "");
  }

  async function send() {
    if (!draft) return;
    setBusy(true);
    setError(null);
    const keepsNote = !!draft.ai_note && text.includes(draft.ai_note);
    const result = await sendExceptionMessage(item.id, {
      category: draft.category,
      template_id: draft.template_id,
      locale: draft.locale,
      message: text,
      recipient_email: recipient,
      ai_note: keepsNote,
      model_call_id: keepsNote ? (draft.model_call_id ?? null) : null,
    });
    setBusy(false);
    if (!result.ok) return setError(result.code);
    setSent(result.message);
  }

  if (sent) {
    return (
      <div role="status" className="flex items-center gap-2 rounded-lg border p-3 text-sm" data-message-sent={sent.status}>
        <CheckCircle2 aria-hidden className="size-4" />
        {sent.status === "awaiting_approval" ? t("awaitingApproval", { reference: sent.reference }) : t("messageFailed")}
      </div>
    );
  }

  return (
    <div className="space-y-3 rounded-lg border bg-background p-4" data-composer={item.reference}>
      <div className="flex flex-wrap items-end gap-3">
        <div className="space-y-1">
          <Label htmlFor={`${id}-template`}>{t("template")}</Label>
          <select
            id={`${id}-template`}
            className="h-9 rounded-md border bg-background px-2 text-sm"
            value={choice}
            onChange={(e) => {
              setChoice(e.target.value);
              setDraft(null);
            }}
          >
            {options.map((o) => (
              <option key={`${o.category}:${o.id}`} value={`${o.category}:${o.id}`}>
                {t(`templates.${o.id}` as "templates.po_missing")} ({t(`recipient.${o.recipient}`)})
              </option>
            ))}
          </select>
        </div>
        <div className="space-y-1">
          <Label htmlFor={`${id}-lang`}>{t("language")}</Label>
          <select
            id={`${id}-lang`}
            className="h-9 rounded-md border bg-background px-2 text-sm"
            value={lang}
            onChange={(e) => {
              setLang(e.target.value === "ar" ? "ar" : "en");
              setDraft(null);
            }}
          >
            <option value="en">English</option>
            <option value="ar">العربية</option>
          </select>
        </div>
        <label className="flex items-center gap-2 text-sm">
          <Checkbox checked={personalise} onChange={(e) => setPersonalise(e.target.checked)} />
          {t("personalise")}
        </label>
        <Button type="button" size="sm" variant="outline" onClick={makeDraft} disabled={busy || !choice}>
          {busy && !draft ? <Loader2 aria-hidden className="animate-spin" /> : <Mail aria-hidden />}
          {t("draft")}
        </Button>
        <Button type="button" size="sm" variant="ghost" onClick={onClose}>
          {t("close")}
        </Button>
      </div>
      {draft && (
        <div className="space-y-3">
          {draft.ai_problems.length > 0 && (
            <p className="text-xs text-muted-foreground" data-ai-problems>
              {t("noPersonalNote")}
            </p>
          )}
          {draft.ai_note && <p className="text-xs text-muted-foreground">{t("personalNoteFromModel")}</p>}
          <div className="space-y-1">
            <Label htmlFor={`${id}-text`}>{t("message")}</Label>
            <textarea
              id={`${id}-text`}
              dir={draft.locale === "ar" ? "rtl" : "ltr"}
              className="min-h-40 w-full rounded-md border bg-background p-2 text-sm"
              value={text}
              maxLength={3000}
              onChange={(e) => setText(e.target.value)}
            />
          </div>
          <div className="space-y-1">
            <Label htmlFor={`${id}-to`}>{t("to")}</Label>
            {draft.recipient === "vendor" ? (
              draft.contacts.length === 0 ? (
                <p className="text-sm" data-no-contacts>
                  {t("noContacts")}
                </p>
              ) : (
                <select
                  id={`${id}-to`}
                  className="h-9 rounded-md border bg-background px-2 text-sm"
                  value={recipient}
                  onChange={(e) => setRecipient(e.target.value)}
                >
                  {draft.contacts.map((c) => (
                    <option key={c.email} value={c.email}>
                      {c.name} ({c.email})
                    </option>
                  ))}
                </select>
              )
            ) : (
              <>
                <input
                  id={`${id}-to`}
                  type="email"
                  dir="ltr"
                  className="h-9 w-full max-w-sm rounded-md border bg-background px-2 text-sm"
                  value={recipient}
                  onChange={(e) => setRecipient(e.target.value)}
                />
                <p className="text-xs text-muted-foreground">
                  {draft.requester_hint ? t("requesterHint", { name: draft.requester_hint }) : t("memberOnly")}
                </p>
              </>
            )}
          </div>
          <p className="text-xs text-muted-foreground">{t("approvalNote")}</p>
          <Button type="button" size="sm" onClick={send} disabled={busy || !text.trim() || !recipient.trim()}>
            {busy ? <Loader2 aria-hidden className="animate-spin" /> : <Send aria-hidden />}
            {t("sendForApproval")}
          </Button>
        </div>
      )}
      <ErrorText code={error} />
    </div>
  );
}

/**
 * The exceptions queue (brief 4.5.4): invoices waiting for a person, by category, each
 * with the checks' own explanation, a suggested resolution and how long it has waited.
 * J and K move through the list, Enter opens the invoice. A message to the vendor or the
 * requester is drafted from a template and sent only after another person approves it.
 */
export function ExceptionsQueue({
  data,
  locale,
  initialCategory,
}: {
  data: PayExceptions;
  locale: string;
  initialCategory: ExceptionCategoryKey | null;
}) {
  const t = useTranslations("Pay.exceptions");
  const router = useRouter();
  const [category, setCategory] = useState<ExceptionCategoryKey | null>(initialCategory);
  const [selected, setSelected] = useState(0);
  const [composing, setComposing] = useState<string | null>(null);
  const rows = useRef<Record<string, HTMLLIElement | null>>({});
  const items = itemsOf(data.items, category);
  const lang: Lang = locale === "ar" ? "ar" : "en";

  useEffect(() => {
    function onKey(event: KeyboardEvent) {
      if (event.altKey || event.ctrlKey || event.metaKey || typingIn(event.target)) return;
      if (event.key === "Enter") {
        const item = items[selected];
        if (item && !(event.target instanceof HTMLButtonElement || event.target instanceof HTMLAnchorElement)) {
          event.preventDefault();
          router.push(`/pay/invoices/${item.id}`);
        }
        return;
      }
      if (!["j", "J", "k", "K"].includes(event.key)) return;
      event.preventDefault();
      const next = move(selected, event.key, items.length);
      setSelected(next);
      const item = items[next];
      if (item) rows.current[item.id]?.scrollIntoView?.({ block: "nearest" });
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [items, selected, router]);

  function choose(next: ExceptionCategoryKey | null) {
    setCategory(next);
    setSelected(0);
    setComposing(null);
  }

  return (
    <div className="space-y-5" data-exceptions>
      <div className="flex flex-wrap items-center gap-3 text-sm">
        <span data-exceptions-total>{t("total", { count: data.total })}</span>
        <span className="text-muted-foreground" data-exceptions-overdue>
          {t("overdueCount", { count: data.overdue, hours: data.sla_hours })}
        </span>
        <span className="text-xs text-muted-foreground">{t("keys")}</span>
      </div>
      <div role="group" aria-label={t("filter")} className="flex flex-wrap gap-2">
        <Button type="button" size="sm" variant={category === null ? "secondary" : "outline"} aria-pressed={category === null} onClick={() => choose(null)}>
          {t("all")}
        </Button>
        {EXCEPTION_CATEGORIES.map((key) => (
          <Button
            key={key}
            type="button"
            size="sm"
            variant={category === key ? "secondary" : "outline"}
            aria-pressed={category === key}
            data-filter={key}
            onClick={() => choose(key)}
          >
            {t(`category.${key}`)}
            <span className="tabular-nums text-muted-foreground">{data.counts[key] ?? 0}</span>
          </Button>
        ))}
      </div>
      {items.length === 0 ? (
        <EmptyState icon={<Inbox aria-hidden />} title={t("emptyTitle")} body={t("emptyBody")} />
      ) : (
        <ul className="space-y-3" aria-label={t("list")}>
          {items.map((item, index) => (
            <li
              key={item.id}
              ref={(node) => {
                rows.current[item.id] = node;
              }}
              aria-current={index === selected ? "true" : undefined}
              data-exception={item.reference}
              onClick={() => setSelected(index)}
              className={cn(
                "space-y-3 rounded-xl border bg-surface p-4",
                index === selected && "ring-2 ring-ring",
              )}
            >
              <div className="flex flex-wrap items-baseline justify-between gap-2">
                <div className="space-y-0.5">
                  <Link href={`/pay/invoices/${item.id}`} className="font-medium underline-offset-4 hover:underline">
                    <bdi>{item.invoice_number ?? item.reference}</bdi>
                  </Link>
                  <p className="text-xs text-muted-foreground">
                    <span dir="ltr">{item.reference}</span>
                    {item.vendor_name && (
                      <>
                        {" · "}
                        <bdi>{item.vendor_name}</bdi>
                      </>
                    )}
                  </p>
                </div>
                <div className="flex flex-wrap items-center gap-3">
                  {item.total !== null && item.total !== undefined && (
                    <span className="font-mono text-sm tabular-nums" dir="ltr">
                      {item.currency} {item.total}
                    </span>
                  )}
                  <Age hours={item.age_hours} overdue={item.overdue} />
                </div>
              </div>
              <div className="space-y-2">
                {item.categories.map((c) => (
                  <div key={c.category} className="space-y-1" data-category={c.category}>
                    <Badge variant={c.category === "bank_mismatch" ? "signal" : "outline"}>{t(`category.${c.category}`)}</Badge>
                    {c.explanations.length > 0 && (
                      <ul className="list-disc space-y-0.5 ps-5 text-sm">
                        {c.explanations.map((e, i) => (
                          <li key={i}>{lang === "ar" ? e.ar : e.en}</li>
                        ))}
                      </ul>
                    )}
                    <p className="text-sm text-muted-foreground" data-resolution>
                      <span className="font-medium text-foreground">{t("resolution")}</span>{" "}
                      {lang === "ar" ? c.resolution.ar : c.resolution.en}
                    </p>
                  </div>
                ))}
              </div>
              {data.can_message &&
                item.categories.some((c) => c.templates.length > 0) &&
                (composing === item.id ? (
                  <Composer item={item} onClose={() => setComposing(null)} />
                ) : (
                  <Button type="button" size="sm" variant="outline" onClick={() => setComposing(item.id)}>
                    <Mail aria-hidden />
                    {t("writeMessage")}
                  </Button>
                ))}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
