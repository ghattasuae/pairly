// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
"use client";

import { ArrowLeft, Loader2, Send, ShieldCheck, Sparkles } from "lucide-react";
import { useTranslations } from "next-intl";
import { useEffect, useId, useRef, useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Checkbox, Textarea } from "@/components/ui/form-controls";
import { Input } from "@/components/ui/input";
import { useToast } from "@/components/ui/toaster";
import {
  aiDraftMessage,
  cancelMessage,
  editMessage,
  sendMessage,
  submitMessage,
} from "@/lib/actions/hire";
import { actionKey } from "@/lib/hire/board";
import type { HireMessage } from "@/lib/hire/types";
import { cn } from "@/lib/utils";

const EDITABLE = new Set(["draft", "changes_requested"]);

export interface ComposeActions {
  edit: typeof editMessage;
  ai: typeof aiDraftMessage;
  send: typeof sendMessage;
  submit: typeof submitMessage;
  cancel: typeof cancelMessage;
}

const DEFAULT_ACTIONS: ComposeActions = {
  edit: editMessage,
  ai: aiDraftMessage,
  send: sendMessage,
  submit: submitMessage,
  cancel: cancelMessage,
};

/**
 * Mount one per message (`key={message.id}`): the panel keeps its own copy as it edits.
 *
 * The compose panel beside the candidate profile (brief 5.3, Messaging UX): the template,
 * the fields a person fills (the AI can draft the personal paragraph), and an exact
 * preview of what the candidate will read. Then one button: "Send" for an update, or
 * "Submit for approval" for a rejection or an offer, which goes to the approval queue and
 * is sent only after another person approves it. The preview is the confirmation:
 * nothing else asks again.
 */
export function ComposePanel({
  message: initial,
  onBack,
  onDone,
  onError,
  actions = DEFAULT_ACTIONS,
}: {
  message: HireMessage;
  onBack: () => void;
  /** After a send, a submission or a cancel: the profile reloads. */
  onDone: (message: HireMessage) => void;
  onError: (code: string) => void;
  actions?: ComposeActions;
}) {
  const t = useTranslations("Hire");
  const tErrors = useTranslations("Errors");
  const toast = useToast();
  const ids = useId();
  const [message, setMessage] = useState(initial);
  const [values, setValues] = useState<Record<string, string>>(() => stringValues(initial.values));
  const [dirty, setDirty] = useState(false);
  const [pending, setPending] = useState<null | "save" | "ai" | "send" | "cancel">(null);
  const [withReason, setWithReason] = useState(false);
  const [aiNote, setAiNote] = useState<string | null>(null);
  const sendKey = useRef(actionKey());
  const primaryRef = useRef<HTMLButtonElement | null>(null);

  // Ready to act at once: the primary button has focus (Enter sends or submits).
  useEffect(() => {
    if (EDITABLE.has(initial.status)) primaryRef.current?.focus({ preventScroll: true });
  }, [initial.id, initial.status]);

  const editable = EDITABLE.has(message.status);

  const save = async (): Promise<HireMessage | null> => {
    if (!dirty) return message;
    setPending("save");
    const result = await actions.edit(message.id, toValues(values));
    setPending(null);
    if (!result.ok) {
      onError(result.code);
      return null;
    }
    setMessage(result.message);
    setDirty(false);
    return result.message;
  };

  const draftWithAi = async () => {
    const saved = await save();
    if (!saved) return;
    setPending("ai");
    setAiNote(null);
    const result = await actions.ai(message.id, withReason);
    setPending(null);
    if (!result.ok) {
      setAiNote(result.code === "bad_request" ? t("compose.aiRefused") : t("compose.aiUnavailable"));
      return;
    }
    setMessage(result.draft.message);
    setValues(stringValues(result.draft.message.values));
    setAiNote(t("compose.aiDone"));
  };

  const go = async () => {
    const saved = await save();
    if (!saved) return;
    if (!saved.preview.ok) {
      onError("invalid_input");
      return;
    }
    setPending("send");
    const result = saved.sensitive
      ? await actions.submit(saved.id, sendKey.current)
      : await actions.send(saved.id, sendKey.current);
    setPending(null);
    if (!result.ok) {
      onError(result.code);
      return;
    }
    sendKey.current = actionKey();
    setMessage(result.message);
    toast.show({
      tone: "success",
      title:
        result.message.status === "suppressed"
          ? t("compose.suppressed")
          : saved.sensitive
            ? t("compose.submitted")
            : t("compose.sent"),
      body: saved.sensitive ? t("compose.submittedBody") : undefined,
    });
    onDone(result.message);
  };

  const cancel = async () => {
    setPending("cancel");
    const result = await actions.cancel(message.id);
    setPending(null);
    if (!result.ok) {
      onError(result.code);
      return;
    }
    onDone(result.message);
  };

  const preview = message.preview;
  const dir = message.locale === "ar" ? "rtl" : "ltr";

  return (
    <section aria-labelledby={`${ids}-title`} className="space-y-4" data-compose={message.id}>
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0 space-y-1">
          <button
            type="button"
            onClick={onBack}
            className="inline-flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground"
          >
            <ArrowLeft className="size-3.5 rtl:rotate-180" aria-hidden />
            {t("compose.back")}
          </button>
          <h3 id={`${ids}-title`} className="font-display text-xl leading-snug">
            {message.label}
          </h3>
        </div>
        <div className="flex flex-wrap justify-end gap-1">
          {message.sensitive && (
            <Badge variant="signal" data-sensitive>
              <ShieldCheck className="size-3.5" aria-hidden />
              {t("compose.needsApproval")}
            </Badge>
          )}
          <Badge variant="outline">{t(`messageStatus.${message.status}` as "messageStatus.draft")}</Badge>
        </div>
      </div>

      {message.sensitive && editable && <p className="text-sm text-muted-foreground">{t("compose.sensitiveHelp")}</p>}
      {message.candidate_opted_out && !message.transactional && (
        <p role="note" className="rounded-md border border-signal/50 bg-signal-soft/60 px-3 py-2 text-sm">
          {t("compose.optedOut")}
        </p>
      )}

      {editable && (
        <fieldset disabled={pending !== null} className="space-y-3">
          <legend className="sr-only">{t("compose.fields")}</legend>
          {message.fields.map((field) => {
            const id = `${ids}-${field.name}`;
            const long = field.type === "text";
            const label = t(`fields.${field.name}` as "fields.personal_note");
            return (
              <div key={field.name} className="space-y-1.5">
                <label htmlFor={id} className="flex items-center gap-2 text-sm font-medium">
                  {label}
                  {!field.required && <span className="text-xs font-normal text-muted-foreground">{t("compose.optional")}</span>}
                  {message.ai_fields.includes(field.name) && (
                    <Badge variant="outline" className="gap-1">
                      <Sparkles className="size-3" aria-hidden />
                      {t("compose.aiWritten")}
                    </Badge>
                  )}
                </label>
                {long ? (
                  <Textarea
                    id={id}
                    rows={3}
                    dir="auto"
                    value={values[field.name] ?? ""}
                    maxLength={2000}
                    onChange={(e) => {
                      setValues((all) => ({ ...all, [field.name]: e.target.value }));
                      setDirty(true);
                    }}
                    onBlur={() => void save()}
                  />
                ) : (
                  <Input
                    id={id}
                    dir="auto"
                    type={field.type === "url" ? "url" : "text"}
                    value={values[field.name] ?? ""}
                    maxLength={2000}
                    onChange={(e) => {
                      setValues((all) => ({ ...all, [field.name]: e.target.value }));
                      setDirty(true);
                    }}
                    onBlur={() => void save()}
                  />
                )}
              </div>
            );
          })}
          {message.ai_available && (
            <div className="flex flex-wrap items-center gap-3">
              <Button type="button" variant="outline" size="sm" onClick={() => void draftWithAi()}>
                {pending === "ai" ? <Loader2 className="animate-spin" aria-hidden /> : <Sparkles aria-hidden />}
                {t("compose.aiDraft")}
              </Button>
              {message.fields.some((f) => f.name === "reason") && (
                <label className="flex items-center gap-2 text-xs text-muted-foreground">
                  <Checkbox checked={withReason} onChange={(e) => setWithReason(e.target.checked)} />
                  {t("compose.withReason")}
                </label>
              )}
            </div>
          )}
          {aiNote && (
            <p role="status" className="text-xs text-muted-foreground">
              {aiNote}
            </p>
          )}
        </fieldset>
      )}

      <figure className="space-y-1.5">
        <figcaption className="eyebrow">{t("compose.preview")}</figcaption>
        <div
          className="rounded-lg border bg-surface-sunken/60 p-4 text-sm shadow-inner"
          data-preview
          lang={message.locale}
          dir={dir}
        >
          {preview.ok ? (
            <>
              <p className="mb-3 border-b pb-2 font-medium">{preview.subject}</p>
              <p className="whitespace-pre-wrap leading-relaxed">{preview.text}</p>
            </>
          ) : (
            <p className="text-muted-foreground">
              {preview.missing.length > 0
                ? t("compose.missing", {
                    fields: preview.missing.map((f) => t(`fields.${f}` as "fields.personal_note")).join(", "),
                  })
                : tErrors("generic")}
            </p>
          )}
        </div>
        {preview.masked && <p className="text-xs text-muted-foreground">{t("compose.masked")}</p>}
      </figure>

      {editable ? (
        <div className="flex flex-wrap items-center gap-2">
          <Button
            ref={primaryRef}
            type="button"
            onClick={() => void go()}
            disabled={pending !== null}
            variant={message.sensitive ? "signal" : "default"}
            data-primary-action
          >
            {pending === "send" ? (
              <Loader2 className="animate-spin" aria-hidden />
            ) : message.sensitive ? (
              <ShieldCheck aria-hidden />
            ) : (
              <Send aria-hidden className="rtl:-scale-x-100" />
            )}
            {message.sensitive ? t("compose.submit") : t("compose.send")}
          </Button>
          <Button type="button" variant="ghost" onClick={() => void cancel()} disabled={pending !== null}>
            {t("compose.discard")}
          </Button>
        </div>
      ) : (
        <p className={cn("text-sm", message.status === "failed" && "text-destructive")}>
          {t(`messageHelp.${message.status}` as "messageHelp.queued")}
        </p>
      )}
    </section>
  );
}

function stringValues(values: Record<string, unknown>): Record<string, string> {
  const out: Record<string, string> = {};
  for (const [k, v] of Object.entries(values)) if (typeof v === "string") out[k] = v;
  return out;
}

function toValues(values: Record<string, string>): Record<string, string | null> {
  const out: Record<string, string | null> = {};
  for (const [k, v] of Object.entries(values)) out[k] = v.trim() ? v : null;
  return out;
}
