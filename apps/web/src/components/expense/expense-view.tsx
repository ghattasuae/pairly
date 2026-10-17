// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
"use client";

import { FileUp, Loader2, Receipt } from "lucide-react";
import { useFormatter, useLocale, useTranslations } from "next-intl";
import { useId, useState } from "react";
import type * as React from "react";

import { ActionError } from "@/components/settings/admin-panels";
import { EmptyState } from "@/components/states/empty-state";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/form-controls";
import { Input } from "@/components/ui/input";
import { useToast } from "@/components/ui/toaster";
import { useRouter } from "@/i18n/navigation";
import type { ActionResult } from "@/lib/action-result";
import { MAX_NOTE_LENGTH } from "@/lib/expense-upload";
import { submitExpenseNote } from "@/lib/expense-upload-client";

export interface ExpenseNoteRow {
  id: string;
  status: string;
  merchant: string | null;
  amount: string | null;
  currency: string | null;
  created_at: string;
  approval_id: string | null;
}

const STATUSES = ["processing", "awaiting_approval", "approved", "rejected", "changes_requested", "failed", "closed"];

export interface ExpenseViewProps {
  notes: ExpenseNoteRow[];
  canSubmit: boolean;
  canReview: boolean;
  totpEnabled: boolean;
  submit?: (form: FormData, key: string) => Promise<ActionResult<{ id: string }>>;
}

/**
 * Expense notes (the M1 example workflow): submit a receipt with a short note, and see
 * your notes (reviewers see everyone's). Every note goes to a person through the inbox.
 */
export function ExpenseView({ notes, canSubmit, canReview, totpEnabled, submit = submitExpenseNote }: ExpenseViewProps) {
  const t = useTranslations("Expense");
  const format = useFormatter();
  const locale = useLocale();
  const router = useRouter();
  const toast = useToast();
  const ids = { file: useId(), note: useId() };
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const onSubmit = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const form = event.currentTarget;
    setPending(true);
    setError(null);
    try {
      const result = await submit(new FormData(form), crypto.randomUUID());
      if (result.ok) {
        form.reset();
        toast.show({ tone: "success", title: t("submitted"), body: t("submittedBody") });
        router.refresh();
      } else setError(result.code);
    } catch {
      setError("unavailable");
    }
    setPending(false);
  };

  return (
    <div className="space-y-10">
      <ActionError code={error} returnPath={`/${locale}/expense`} totpEnabled={totpEnabled} />
      {canSubmit && (
        <form onSubmit={onSubmit} className="space-y-4 rounded-xl border bg-surface p-5" aria-labelledby="expense-upload">
          <div className="space-y-1">
            <h2 id="expense-upload" className="font-display text-xl">
              {t("uploadTitle")}
            </h2>
            <p className="text-sm text-muted-foreground">{t("uploadBody")}</p>
          </div>
          <fieldset disabled={pending} className="grid gap-4">
            <div className="space-y-2">
              <label htmlFor={ids.file} className="text-sm font-medium">
                {t("file")}
              </label>
              <Input
                id={ids.file}
                name="file"
                type="file"
                required
                accept=".pdf,.png,.jpg,.jpeg,application/pdf,image/png,image/jpeg"
                className="h-auto py-2"
              />
            </div>
            <div className="space-y-2">
              <label htmlFor={ids.note} className="text-sm font-medium">
                {t("note")}
              </label>
              <Textarea id={ids.note} name="note" maxLength={MAX_NOTE_LENGTH} rows={2} placeholder={t("notePlaceholder")} />
            </div>
          </fieldset>
          <Button type="submit" disabled={pending}>
            {pending ? <Loader2 className="animate-spin" aria-hidden /> : <FileUp aria-hidden />}
            {pending ? t("submitting") : t("submit")}
          </Button>
        </form>
      )}

      <section aria-labelledby="expense-notes" className="space-y-3">
        <h2 id="expense-notes" className="eyebrow">
          {canReview ? t("allNotes") : t("myNotes")}
        </h2>
        {notes.length === 0 ? (
          <EmptyState icon={<Receipt aria-hidden />} title={t("empty")} body={t("emptyBody")} />
        ) : (
          <ul className="divide-y rounded-xl border bg-surface">
            {notes.map((note) => (
              <li key={note.id} className="flex flex-wrap items-center justify-between gap-3 px-4 py-3 text-sm">
                <div className="min-w-0 space-y-0.5">
                  <p className="font-medium">{note.merchant ?? t("unread")}</p>
                  <p className="text-xs text-muted-foreground">
                    {format.dateTime(new Date(note.created_at), { dateStyle: "medium", timeStyle: "short" })}
                  </p>
                </div>
                <div className="flex items-center gap-3">
                  {note.amount !== null && note.currency !== null && (
                    <span className="font-mono tabular-nums" dir="ltr">
                      {note.amount} {note.currency}
                    </span>
                  )}
                  <Badge variant={note.status === "awaiting_approval" ? "signal" : "outline"}>
                    {STATUSES.includes(note.status) ? t(`status.${note.status}` as "status.processing") : note.status}
                  </Badge>
                </div>
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}
