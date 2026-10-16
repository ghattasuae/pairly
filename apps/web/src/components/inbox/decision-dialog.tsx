// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
"use client";

import { useTranslations } from "next-intl";
import { useId } from "react";

import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { Textarea } from "@/components/ui/form-controls";
import type { DecisionChoice } from "@/lib/api/types";
import { needsReason } from "@/lib/inbox-filters";

/**
 * One dialog for the three decisions. Approve and reject are external and final, so the
 * dialog is a confirmation (brief 5.3); reject and request changes need a written reason,
 * which the person who submitted the item will read.
 */
export function DecisionDialog({
  decision,
  title,
  reason,
  onReasonChange,
  onConfirm,
  onClose,
}: {
  decision: DecisionChoice | null;
  /** The approval's title, quoted in the dialog so nobody approves the wrong thing. */
  title: string;
  reason: string;
  onReasonChange: (value: string) => void;
  onConfirm: () => void;
  onClose: () => void;
}) {
  const t = useTranslations("Inbox.dialog");
  const reasonId = useId();
  const hintId = useId();
  const kind = decision ?? "approve";
  const reasonNeeded = needsReason(kind);
  const tooShort = reasonNeeded && reason.trim().length === 0;

  return (
    <ConfirmDialog
      open={decision !== null}
      onOpenChange={(open) => !open && onClose()}
      title={t(`${kind}.title`)}
      description={t(`${kind}.body`, { title })}
      confirmLabel={t(`${kind}.confirm`)}
      tone={kind === "reject" ? "destructive" : "default"}
      confirmDisabled={tooShort}
      onConfirm={onConfirm}
    >
      {reasonNeeded ? (
        <div className="space-y-2">
          <label htmlFor={reasonId} className="text-sm font-medium">
            {t("reasonLabel")}
          </label>
          <Textarea
            id={reasonId}
            autoFocus
            required
            maxLength={2000}
            value={reason}
            onChange={(event) => onReasonChange(event.target.value)}
            aria-describedby={hintId}
            onKeyDown={(event) => {
              // Cmd/Ctrl+Enter sends from the text box; plain Enter adds a line.
              if (event.key === "Enter" && (event.metaKey || event.ctrlKey) && !tooShort) {
                event.preventDefault();
                onConfirm();
              }
            }}
          />
          <p id={hintId} className="text-xs text-muted-foreground">
            {t("reasonHint")}
          </p>
        </div>
      ) : null}
    </ConfirmDialog>
  );
}
