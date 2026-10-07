// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
"use client";

import { useTranslations } from "next-intl";
import type * as React from "react";

import { Button } from "./button";
import { Dialog, DialogContent, DialogDescription, DialogTitle } from "./dialog";

/**
 * Confirmation for actions that cannot be undone or that leave Pairly (brief 5.3:
 * confirm only what is irreversible or external). Optional fields (a reason) go in
 * `children`. Enter submits, Escape cancels, focus starts on the first field or on the
 * confirm button.
 */
export function ConfirmDialog({
  open,
  onOpenChange,
  title,
  description,
  confirmLabel,
  tone = "default",
  pending = false,
  confirmDisabled = false,
  onConfirm,
  children,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: React.ReactNode;
  description?: React.ReactNode;
  confirmLabel: React.ReactNode;
  tone?: "default" | "destructive";
  pending?: boolean;
  confirmDisabled?: boolean;
  onConfirm: () => void;
  children?: React.ReactNode;
}) {
  const t = useTranslations("Common");
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-md">
        <form
          className="space-y-5 p-6"
          onSubmit={(event) => {
            event.preventDefault();
            if (!pending && !confirmDisabled) onConfirm();
          }}
        >
          <div className="space-y-2">
            <DialogTitle className="font-display text-2xl leading-tight">{title}</DialogTitle>
            {description ? (
              <DialogDescription className="text-sm text-muted-foreground">{description}</DialogDescription>
            ) : (
              <DialogDescription className="sr-only">{title}</DialogDescription>
            )}
          </div>
          {children}
          <div className="flex flex-col-reverse gap-2 sm:flex-row sm:justify-end">
            <Button type="button" variant="ghost" onClick={() => onOpenChange(false)} disabled={pending}>
              {t("cancel")}
            </Button>
            <Button
              type="submit"
              variant={tone === "destructive" ? "destructive" : "default"}
              disabled={pending || confirmDisabled}
              autoFocus={!children}
            >
              {confirmLabel}
            </Button>
          </div>
        </form>
      </DialogContent>
    </Dialog>
  );
}
