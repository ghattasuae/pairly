// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
"use client";

import { Loader2, ReceiptText, Send } from "lucide-react";
import { useFormatter, useTranslations } from "next-intl";
import { useId, useState } from "react";

import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/form-controls";
import { applyCreditNote, replyInPortal } from "@/lib/actions/pay";
import type { PayThreadMessage } from "@/lib/pay/types";

import { ErrorText, Panel } from "./common";

export interface PortalThreadActions {
  reply: typeof replyInPortal;
  apply: typeof applyCreditNote;
}

const DEFAULT_ACTIONS: PortalThreadActions = { reply: replyInPortal, apply: applyCreditNote };

/**
 * The vendor portal thread of an invoice for accounts payable (brief 4.7): what the
 * vendor's contacts wrote and AP's replies. A reply is shown in the portal at once (the
 * default reply template needs no approval); when the organisation made it sensitive, it
 * waits for an approval in the inbox first. For a credit note, AP applies it here.
 */
export function PortalThread({
  invoiceId,
  initial,
  canReply,
  creditNote,
  actions = DEFAULT_ACTIONS,
}: {
  invoiceId: string;
  initial: PayThreadMessage[];
  canReply: boolean;
  creditNote: { canApply: boolean; forInvoice: string | null; applied: boolean } | null;
  actions?: PortalThreadActions;
}) {
  const t = useTranslations("Pay.portal");
  const format = useFormatter();
  const ids = useId();
  const [thread, setThread] = useState(initial);
  const [text, setText] = useState("");
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState<null | "reply" | "apply">(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [applied, setApplied] = useState(creditNote?.applied ?? false);

  const reply = async () => {
    if (!text.trim()) return;
    setBusy("reply");
    setError(null);
    const result = await actions.reply(invoiceId, text.trim());
    setBusy(null);
    if (!result.ok) return setError(result.code);
    setThread(result.thread);
    setText("");
    const last = result.thread.at(-1);
    setNotice(last && last.status !== "posted" ? t("waitsForApproval") : t("replied"));
  };

  const apply = async () => {
    setBusy("apply");
    setError(null);
    const result = await actions.apply(invoiceId, { invoice_id: null, note });
    setBusy(null);
    if (!result.ok) return setError(result.code);
    setApplied(true);
    setNotice(t("applied"));
  };

  return (
    <Panel title={t("threadTitle")} data-portal-thread>
      <p role="status" aria-live="polite" className="text-sm">
        {notice ?? ""}
      </p>
      <ErrorText code={error} />
      {creditNote && (
        <div className="space-y-2 rounded-lg border p-3" data-credit-note>
          <p className="flex items-center gap-2 text-sm font-medium">
            <ReceiptText className="size-4" aria-hidden />
            {applied ? t("creditApplied") : t("creditNote", { invoice: creditNote.forInvoice ?? t("noInvoice") })}
          </p>
          {!applied && creditNote.canApply && (
            <form
              className="space-y-2"
              onSubmit={(e) => {
                e.preventDefault();
                void apply();
              }}
            >
              <label htmlFor={`${ids}-note`} className="block text-xs font-medium">
                {t("applyNote")}
              </label>
              <Textarea id={`${ids}-note`} rows={2} maxLength={1000} value={note} onChange={(e) => setNote(e.target.value)} />
              <Button type="submit" size="sm" disabled={busy !== null || !creditNote.forInvoice}>
                {busy === "apply" && <Loader2 className="animate-spin" aria-hidden />}
                {t("apply")}
              </Button>
            </form>
          )}
        </div>
      )}
      {thread.length === 0 ? (
        <p className="text-sm text-muted-foreground">{t("noMessages")}</p>
      ) : (
        <ol className="space-y-2">
          {thread.map((m) => (
            <li
              key={m.id}
              className={m.from_vendor ? "me-6 rounded-lg border p-3" : "ms-6 rounded-lg border bg-accent/40 p-3"}
              data-thread-message={m.from_vendor ? "vendor" : "ap"}
              data-status={m.status}
            >
              <p className="text-xs text-muted-foreground">
                {m.from_vendor ? m.author || t("vendor") : t("ap")}
                {" · "}
                {format.dateTime(new Date(m.at), { dateStyle: "medium", timeStyle: "short" })}
                {!m.from_vendor && m.status !== "posted" && m.status !== "queued" && ` · ${t(`replyStatus.${m.status}` as "replyStatus.submitted")}`}
              </p>
              <p className="mt-1 text-sm whitespace-pre-wrap">{m.text}</p>
            </li>
          ))}
        </ol>
      )}
      {canReply && (
        <form
          className="space-y-2"
          onSubmit={(e) => {
            e.preventDefault();
            void reply();
          }}
        >
          <label htmlFor={`${ids}-reply`} className="block text-sm font-medium">
            {t("replyLabel")}
          </label>
          <Textarea id={`${ids}-reply`} rows={3} maxLength={4000} value={text} onChange={(e) => setText(e.target.value)} />
          <Button type="submit" disabled={busy !== null || !text.trim()}>
            {busy === "reply" ? <Loader2 className="animate-spin" aria-hidden /> : <Send className="rtl:-scale-x-100" aria-hidden />}
            {t("reply")}
          </Button>
        </form>
      )}
    </Panel>
  );
}
