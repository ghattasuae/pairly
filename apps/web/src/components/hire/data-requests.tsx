// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
"use client";

import { useFormatter, useTranslations } from "next-intl";
import { useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { Textarea } from "@/components/ui/form-controls";
import { errorKey } from "@/lib/action-result";
import { eraseDataRequest, loadDataRequest, resolveDataRequest } from "@/lib/actions/data-requests";
import type { HireDataRequest } from "@/lib/hire/types";

/**
 * The admins' list of candidate data requests (deletion), open ones first, with the
 * deadline of each. Opening one shows the candidate's note and what the admin can do:
 * record it as completed or declined, or (deletion only) erase the candidate's personal
 * data by hand, with a written reason and a confirmation. Nothing is ever erased on its
 * own; the API requires a second factor and audits every step.
 */
export function DataRequests({
  requests,
  initialId = null,
  load = loadDataRequest,
  resolve = resolveDataRequest,
  erase = eraseDataRequest,
  now = new Date(),
}: {
  requests: HireDataRequest[];
  initialId?: string | null;
  load?: typeof loadDataRequest;
  resolve?: typeof resolveDataRequest;
  erase?: typeof eraseDataRequest;
  now?: Date;
}) {
  const t = useTranslations("HireDataRequests");
  const tErr = useTranslations("Errors");
  const format = useFormatter();
  const [rows, setRows] = useState(requests);
  const [openId, setOpenId] = useState<string | null>(
    initialId && requests.some((r) => r.id === initialId) ? initialId : null,
  );
  const [text, setText] = useState("");
  const [reason, setReason] = useState("");
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);

  const current = rows.find((r) => r.id === openId) ?? null;
  const day = (iso: string) => format.dateTime(new Date(iso), { dateStyle: "medium" });
  const overdue = (r: HireDataRequest) => r.status === "open" && new Date(r.due_at) < now;

  const replace = (next: HireDataRequest) => setRows((prev) => prev.map((r) => (r.id === next.id ? next : r)));

  const open = async (id: string) => {
    setOpenId(id);
    setText("");
    setReason("");
    setMessage(null);
    try {
      window.history.replaceState(null, "", `${window.location.pathname}?request=${id}`);
    } catch {
      // The address bar is a convenience only.
    }
    const fresh = await load(id).catch(() => null);
    if (fresh?.ok) replace(fresh.request);
  };

  const done = (result: Awaited<ReturnType<typeof resolve>>, ok: string) => {
    setBusy(false);
    setConfirming(false);
    if (result.ok) {
      replace(result.request);
      setMessage(ok);
    } else {
      setMessage(tErr(errorKey(result.code) as "generic"));
    }
  };

  const record = async (status: "completed" | "declined") => {
    if (!current) return;
    setBusy(true);
    done(await resolve({ id: current.id, status, outcome: text }), t("recorded"));
  };

  const eraseNow = async () => {
    if (!current) return;
    setBusy(true);
    done(await erase({ id: current.id, reason }), t("erased"));
  };

  if (rows.length === 0) {
    return <p className="text-sm text-muted-foreground">{t("empty")}</p>;
  }

  return (
    <div className="grid gap-6 md:grid-cols-[minmax(0,1fr)_minmax(0,1.2fr)]">
      <ul className="divide-y divide-border rounded-md border border-border" aria-label={t("listLabel")}>
        {rows.map((r) => (
          <li key={r.id}>
            <button
              type="button"
              onClick={() => void open(r.id)}
              aria-current={r.id === openId ? "true" : undefined}
              className="flex w-full flex-col gap-1 px-4 py-3 text-start text-sm hover:bg-accent aria-[current=true]:bg-accent"
            >
              <span className="flex flex-wrap items-center gap-2">
                <span className="font-medium">{t(`kind.${r.kind === "deletion" ? "deletion" : "other"}`)}</span>
                <Badge variant={r.status === "open" ? "signal" : "outline"}>
                  {t(`status.${r.status === "open" || r.status === "completed" ? r.status : "declined"}`)}
                </Badge>
                {overdue(r) && <Badge className="text-destructive">{t("overdue")}</Badge>}
              </span>
              <span className="text-muted-foreground">
                {r.status === "open" ? t("due", { date: day(r.due_at) }) : t("handled", { date: day(r.handled_at ?? r.created_at) })}
              </span>
            </button>
          </li>
        ))}
      </ul>

      {current ? (
        <section className="space-y-4 rounded-md border border-border p-4 text-sm" aria-label={t("detailLabel")}>
          <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-2">
            <dt className="text-muted-foreground">{t("candidate")}</dt>
            <dd className="min-w-0 break-words">
              {current.candidate_name}
              <span className="block text-muted-foreground" dir="ltr">
                {current.candidate_email}
              </span>
            </dd>
            <dt className="text-muted-foreground">{t("received")}</dt>
            <dd>{day(current.created_at)}</dd>
            <dt className="text-muted-foreground">{t("deadline")}</dt>
            <dd>{day(current.due_at)}</dd>
            {current.note && (
              <>
                <dt className="text-muted-foreground">{t("note")}</dt>
                <dd className="whitespace-pre-wrap break-words">{current.note}</dd>
              </>
            )}
            {current.outcome && (
              <>
                <dt className="text-muted-foreground">{t("outcome")}</dt>
                <dd className="whitespace-pre-wrap break-words">{current.outcome}</dd>
              </>
            )}
          </dl>

          {current.status === "open" && (
            <div className="space-y-5">
              <label className="block space-y-1">
                <span className="block font-medium">{t("recordLabel")}</span>
                <span className="block text-xs text-muted-foreground">{t("recordHelp")}</span>
                <Textarea value={text} onChange={(e) => setText(e.target.value)} maxLength={2000} disabled={busy} />
              </label>
              <div className="flex flex-wrap gap-2">
                <Button
                  size="sm"
                  variant="outline"
                  disabled={busy || text.trim().length < 5}
                  onClick={() => void record("completed")}
                >
                  {t("markCompleted")}
                </Button>
                <Button
                  size="sm"
                  variant="outline"
                  disabled={busy || text.trim().length < 5}
                  onClick={() => void record("declined")}
                >
                  {t("markDeclined")}
                </Button>
              </div>

              {current.kind === "deletion" && (
                <div className="space-y-2 rounded-md border border-destructive/40 p-3">
                  <p className="font-medium">{t("eraseTitle")}</p>
                  <p className="text-xs text-muted-foreground">{t("eraseHelp")}</p>
                  <label className="block space-y-1">
                    <span className="block">{t("reasonLabel")}</span>
                    <Textarea
                      value={reason}
                      onChange={(e) => setReason(e.target.value)}
                      maxLength={2000}
                      disabled={busy}
                    />
                  </label>
                  <Button
                    size="sm"
                    variant="destructive"
                    disabled={busy || reason.trim().length < 10}
                    onClick={() => setConfirming(true)}
                  >
                    {t("erase")}
                  </Button>
                </div>
              )}
            </div>
          )}

          {message && (
            <p role="status" className="text-sm font-medium">
              {message}
            </p>
          )}
        </section>
      ) : (
        <p className="text-sm text-muted-foreground">{t("pick")}</p>
      )}

      <ConfirmDialog
        open={confirming}
        onOpenChange={setConfirming}
        title={t("confirmTitle")}
        description={t("confirmBody")}
        confirmLabel={t("erase")}
        tone="destructive"
        pending={busy}
        onConfirm={() => void eraseNow()}
      />
    </div>
  );
}
