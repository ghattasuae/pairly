// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
"use client";

import { ArrowLeft, CalendarClock, CheckCircle2, FileUp, Loader2, Send, Video } from "lucide-react";
import { useFormatter, useLocale, useTranslations } from "next-intl";
import { useId, useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { Textarea } from "@/components/ui/form-controls";
import { Link } from "@/i18n/navigation";
import { replyToCompany, withdrawApplication } from "@/lib/actions/portal";
import type { PortalApplicationDetail, PortalDocumentRequest } from "@/lib/portal/steps";
import { UPLOAD_HEADER } from "@/lib/upload-guard";

import { StatusTimeline } from "./status-timeline";

const KNOWN_ERRORS = new Set(["not_signed_in", "rate_limited", "conflict", "too_large", "invalid_input"]);

export interface ApplicationDetailActions {
  withdraw: typeof withdrawApplication;
  reply: typeof replyToCompany;
  upload: (requestId: string, file: File) => Promise<{ ok: true } | { ok: false; code: string }>;
}

async function uploadFile(requestId: string, file: File): Promise<{ ok: true } | { ok: false; code: string }> {
  const body = new FormData();
  body.set("file", file);
  try {
    const response = await fetch(`/api/portal/files/${requestId}`, {
      method: "POST",
      body,
      headers: { [UPLOAD_HEADER]: "1" },
      credentials: "same-origin",
    });
    if (response.ok) return { ok: true };
    const problem = (await response.json().catch(() => ({}))) as { code?: string };
    return { ok: false, code: problem.code ?? "unavailable" };
  } catch {
    return { ok: false, code: "unavailable" };
  }
}

const DEFAULT_ACTIONS: ApplicationDetailActions = {
  withdraw: withdrawApplication,
  reply: replyToCompany,
  upload: uploadFile,
};

/**
 * One application in the candidate portal: where it stands, upcoming interviews, the
 * files the team asked for (upload here), the conversation with the hiring team (reply
 * here), and withdrawing it (confirmed first: it cannot be undone here; an email
 * confirms it).
 */
export function ApplicationDetail({
  application: initial,
  actions = DEFAULT_ACTIONS,
}: {
  application: PortalApplicationDetail;
  actions?: ApplicationDetailActions;
}) {
  const t = useTranslations("Portal");
  const format = useFormatter();
  const locale = useLocale();
  const ids = useId();
  const [app, setApp] = useState(initial);
  const [reply, setReply] = useState("");
  const [busy, setBusy] = useState<null | "reply" | "withdraw" | string>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [confirming, setConfirming] = useState(false);

  const explain = (code: string) => t(`errors.${KNOWN_ERRORS.has(code) ? code : "generic"}` as "errors.generic");

  const sendReply = async () => {
    if (!reply.trim()) return;
    setBusy("reply");
    setError(null);
    const result = await actions.reply(app.id, reply.trim());
    setBusy(null);
    if (!result.ok) return setError(explain(result.code));
    setApp((a) => ({ ...a, messages: [...a.messages, result.message] }));
    setReply("");
    setNotice(t("app.sent"));
  };

  const withdraw = async () => {
    setBusy("withdraw");
    setError(null);
    const result = await actions.withdraw(app.id);
    setBusy(null);
    setConfirming(false);
    if (!result.ok) return setError(explain(result.code));
    setApp(result.application);
    setNotice(t("app.withdrawn"));
  };

  const upload = async (request: PortalDocumentRequest, file: File | undefined) => {
    if (!file) return;
    setBusy(request.id);
    setError(null);
    const result = await actions.upload(request.id, file);
    setBusy(null);
    if (!result.ok) return setError(explain(result.code));
    setApp((a) => ({
      ...a,
      open_document_requests: Math.max(0, a.open_document_requests - 1),
      document_requests: a.document_requests.map((r) =>
        r.id === request.id ? { ...r, status: "received", received_at: new Date().toISOString() } : r,
      ),
    }));
    setNotice(t("app.uploaded"));
  };

  return (
    <article aria-labelledby={`${ids}-title`} className="space-y-8" data-portal-detail={app.id} data-status={app.status}>
      <Link href="/portal" className="inline-flex items-center gap-1 text-sm text-primary underline-offset-4 hover:underline">
        <ArrowLeft className="size-4 rtl:rotate-180" aria-hidden />
        {t("app.back")}
      </Link>
      <header className="space-y-1">
        <h1 id={`${ids}-title`} className="font-display text-3xl leading-tight">
          {locale === "ar" && app.job_title_ar ? app.job_title_ar : app.job_title}
        </h1>
        <p className="text-sm text-muted-foreground">
          <span dir="ltr">{t("app.reference", { reference: app.reference })}</span>
          {" · "}
          {t("app.appliedOn", { date: format.dateTime(new Date(app.submitted_at), { dateStyle: "medium" }) })}
        </p>
      </header>

      <p role="status" aria-live="polite" className="text-sm font-medium" data-portal-notice>
        {notice ?? ""}
      </p>
      {error && (
        <p role="alert" className="text-sm text-danger-ink">
          {error}
        </p>
      )}

      <section aria-labelledby={`${ids}-timeline`} className="space-y-3">
        <h2 id={`${ids}-timeline`} className="font-display text-xl">
          {t("app.timeline")}
        </h2>
        <StatusTimeline app={app} />
      </section>

      <section aria-labelledby={`${ids}-interviews`} className="space-y-3">
        <h2 id={`${ids}-interviews`} className="flex items-center gap-2 font-display text-xl">
          <CalendarClock className="size-5" aria-hidden />
          {t("app.interviews")}
        </h2>
        {app.interviews.length === 0 ? (
          <p className="text-sm text-muted-foreground">{t("app.noInterviews")}</p>
        ) : (
          <ul className="space-y-2">
            {app.interviews.map((i) => (
              <li key={i.id} className="space-y-2 card-hairline p-4" data-interview={i.id} data-interview-status={i.status}>
                <p className="font-medium">{t("app.interviewFor", { job: i.title })}</p>
                {i.starts_at ? (
                  <p className="text-sm text-muted-foreground">
                    {format.dateTime(new Date(i.starts_at), { dateStyle: "full", timeStyle: "short", timeZone: i.timezone })}
                    {" "}
                    <span dir="ltr">({i.timezone})</span>
                  </p>
                ) : (
                  <p className="text-sm text-muted-foreground">{t("app.chooseTimeHint", { minutes: i.duration_min })}</p>
                )}
                <div className="flex flex-wrap gap-2">
                  {i.join_url && (
                    <a href={i.join_url} className="inline-flex items-center gap-1 text-sm font-medium text-primary underline" rel="noopener noreferrer" target="_blank">
                      <Video className="size-4" aria-hidden />
                      {t("app.join")}
                    </a>
                  )}
                  {i.choose_url && (
                    <a href={i.choose_url} className="text-sm font-medium text-primary underline" data-interview-choose>
                      {t("app.chooseTime")}
                    </a>
                  )}
                  {i.reschedule_url && (
                    <a href={i.reschedule_url} className="text-sm text-primary underline" data-interview-manage>
                      {i.cancel_url ? t("app.reschedule") : t("app.manageInterview")}
                    </a>
                  )}
                  {i.cancel_url && (
                    <a href={i.cancel_url} className="text-sm text-primary underline">
                      {t("app.cancelInterview")}
                    </a>
                  )}
                </div>
              </li>
            ))}
          </ul>
        )}
      </section>

      {app.has_offer && (
        <Link href={`/portal/applications/${app.id}/offer`} className="block rounded-xl border border-primary/40 bg-primary/5 p-4 font-medium text-primary" data-portal-offer-link>
          {t("app.offer")}
        </Link>
      )}

      <section aria-labelledby={`${ids}-documents`} className="space-y-3">
        <h2 id={`${ids}-documents`} className="flex items-center gap-2 font-display text-xl">
          <FileUp className="size-5" aria-hidden />
          {t("app.documents")}
        </h2>
        {app.document_requests.length === 0 ? (
          <p className="text-sm text-muted-foreground">{t("app.noDocuments")}</p>
        ) : (
          <ul className="space-y-2">
            {app.document_requests.map((r) => {
              const label = locale === "ar" && r.label_ar ? r.label_ar : r.label;
              return (
                <li key={r.id} className="flex flex-wrap items-center justify-between gap-3 card-hairline p-4" data-document-request={r.id}>
                  <span className="font-medium">{label}</span>
                  {r.status === "received" ? (
                    <Badge>
                      <CheckCircle2 className="size-3" aria-hidden />
                      {t("app.received")}
                    </Badge>
                  ) : (
                    <label className="inline-flex cursor-pointer items-center gap-2 rounded-md border px-3 py-1.5 text-sm hover:bg-accent focus-within:outline-2 focus-within:outline-ring">
                      {busy === r.id ? <Loader2 className="size-4 animate-spin" aria-hidden /> : <FileUp className="size-4" aria-hidden />}
                      <span>{busy === r.id ? t("app.uploading") : t("app.upload", { label })}</span>
                      <input
                        type="file"
                        className="sr-only"
                        onChange={(e) => void upload(r, e.target.files?.[0])}
                        disabled={busy !== null}
                      />
                    </label>
                  )}
                </li>
              );
            })}
          </ul>
        )}
      </section>

      <section aria-labelledby={`${ids}-messages`} className="space-y-3">
        <h2 id={`${ids}-messages`} className="font-display text-xl">
          {t("app.messages")}
        </h2>
        {app.messages.length === 0 ? (
          <p className="text-sm text-muted-foreground">{t("app.noMessages")}</p>
        ) : (
          <ol className="space-y-3">
            {app.messages.map((m) => (
              <li
                key={m.id}
                className={m.direction === "from_you" ? "ms-6 rounded-xl border bg-accent/40 p-4" : "me-6 card-hairline p-4"}
                data-message={m.direction}
              >
                <p className="text-xs text-muted-foreground">
                  {m.direction === "from_you" ? t("app.fromYou") : t("app.fromCompany")}
                  {" · "}
                  {format.dateTime(new Date(m.at), { dateStyle: "medium", timeStyle: "short" })}
                </p>
                {m.subject && <p className="mt-1 font-medium">{m.subject}</p>}
                <p className="mt-1 text-sm whitespace-pre-wrap">{m.text}</p>
              </li>
            ))}
          </ol>
        )}
        {!app.final && (
          <form
            className="space-y-2"
            onSubmit={(e) => {
              e.preventDefault();
              void sendReply();
            }}
          >
            <label htmlFor={`${ids}-reply`} className="block text-sm font-medium">
              {t("app.replyLabel")}
            </label>
            <Textarea id={`${ids}-reply`} value={reply} onChange={(e) => setReply(e.target.value)} rows={3} maxLength={8000} />
            <Button type="submit" disabled={busy !== null || !reply.trim()}>
              {busy === "reply" ? <Loader2 className="animate-spin" aria-hidden /> : <Send className="rtl:-scale-x-100" aria-hidden />}
              {t("app.send")}
            </Button>
          </form>
        )}
      </section>

      {app.can_withdraw && (
        <section className="border-t pt-6">
          <Button type="button" variant="outline" onClick={() => setConfirming(true)} data-withdraw>
            {t("app.withdraw")}
          </Button>
        </section>
      )}
      <ConfirmDialog
        open={confirming}
        onOpenChange={setConfirming}
        title={t("app.withdrawTitle")}
        description={t("app.withdrawBody")}
        confirmLabel={t("app.withdrawConfirm")}
        tone="destructive"
        pending={busy === "withdraw"}
        onConfirm={() => void withdraw()}
      />
    </article>
  );
}
