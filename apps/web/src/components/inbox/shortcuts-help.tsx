// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
"use client";

import { useTranslations } from "next-intl";

import {
  Dialog,
  DialogBody,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  dialogTitleClass,
} from "@/components/ui/dialog";
import { Kbd } from "@/components/ui/kbd";

export const INBOX_SHORTCUTS = [
  { keys: ["J"], id: "next" },
  { keys: ["K"], id: "previous" },
  { keys: ["Enter"], id: "open" },
  { keys: ["A"], id: "approve" },
  { keys: ["R"], id: "reject" },
  { keys: ["C"], id: "changes" },
  { keys: ["/"], id: "search" },
  { keys: ["?"], id: "help" },
  { keys: ["Esc"], id: "close" },
] as const;

/** The inbox keyboard shortcuts (opened with ?). */
export function ShortcutsHelp({ open, onOpenChange }: { open: boolean; onOpenChange: (open: boolean) => void }) {
  const t = useTranslations("Inbox.shortcuts");
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-sm overflow-hidden">
        <DialogHeader>
          <DialogTitle className={dialogTitleClass}>{t("title")}</DialogTitle>
          <DialogDescription className="text-sm text-muted-foreground">{t("body")}</DialogDescription>
        </DialogHeader>
        <DialogBody>
          <dl className="divide-y rounded-lg border bg-surface">
            {INBOX_SHORTCUTS.map((shortcut) => (
              <div key={shortcut.id} className="flex items-center justify-between gap-4 px-3 py-2 text-sm">
                <dt>{t(shortcut.id)}</dt>
                <dd className="flex gap-1">
                  {shortcut.keys.map((key) => (
                    <Kbd key={key}>{key}</Kbd>
                  ))}
                </dd>
              </div>
            ))}
          </dl>
        </DialogBody>
      </DialogContent>
    </Dialog>
  );
}
