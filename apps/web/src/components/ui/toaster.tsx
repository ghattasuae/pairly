// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
"use client";

import { CircleAlert, CircleCheck, Info, X } from "lucide-react";
import { useTranslations } from "next-intl";
import { createContext, useCallback, useContext, useMemo, useRef, useState } from "react";
import type * as React from "react";

import { cn } from "@/lib/utils";

export type ToastTone = "success" | "error" | "info";

export interface ToastInput {
  title: string;
  body?: string | undefined;
  tone?: ToastTone;
  /** One optional action, for example "Try again" or "Undo". */
  action?: { label: string; onClick: () => void } | undefined;
  /** Milliseconds before it goes away. Errors stay until closed. */
  duration?: number;
}

interface Toast extends ToastInput {
  id: number;
}

const ToastContext = createContext<{ show: (toast: ToastInput) => void } | null>(null);

/** Toasts for this page. Fallback: a no-op, so components work in tests and stories too. */
export function useToast(): { show: (toast: ToastInput) => void } {
  return useContext(ToastContext) ?? { show: () => {} };
}

const ICONS: Record<ToastTone, React.ReactNode> = {
  success: <CircleCheck className="text-success" aria-hidden />,
  error: <CircleAlert className="text-destructive" aria-hidden />,
  info: <Info className="text-primary" aria-hidden />,
};

/**
 * A quiet stack of messages at the bottom of the screen. Polite announcements for
 * success and info, assertive for errors. Never steals focus.
 */
export function Toaster({ children }: { children: React.ReactNode }) {
  const t = useTranslations("Common");
  const [toasts, setToasts] = useState<Toast[]>([]);
  const next = useRef(1);

  const dismiss = useCallback((id: number) => setToasts((all) => all.filter((toast) => toast.id !== id)), []);

  const show = useCallback(
    (input: ToastInput) => {
      const id = next.current++;
      setToasts((all) => [...all.slice(-3), { ...input, id }]);
      const duration = input.duration ?? (input.tone === "error" ? 0 : 6000);
      if (duration > 0) window.setTimeout(() => dismiss(id), duration);
    },
    [dismiss],
  );

  const value = useMemo(() => ({ show }), [show]);

  return (
    <ToastContext.Provider value={value}>
      {children}
      <div className="pointer-events-none fixed inset-x-0 bottom-4 z-[60] flex flex-col items-center gap-2 px-4 sm:items-end sm:pe-6">
        <div aria-live="polite" className="contents">
          {toasts
            .filter((toast) => toast.tone !== "error")
            .map((toast) => (
              <ToastCard key={toast.id} toast={toast} onClose={() => dismiss(toast.id)} closeLabel={t("close")} />
            ))}
        </div>
        <div aria-live="assertive" className="contents">
          {toasts
            .filter((toast) => toast.tone === "error")
            .map((toast) => (
              <ToastCard key={toast.id} toast={toast} onClose={() => dismiss(toast.id)} closeLabel={t("close")} />
            ))}
        </div>
      </div>
    </ToastContext.Provider>
  );
}

function ToastCard({ toast, onClose, closeLabel }: { toast: Toast; onClose: () => void; closeLabel: string }) {
  return (
    <div
      role={toast.tone === "error" ? "alert" : "status"}
      className={cn(
        "pointer-events-auto flex w-full max-w-sm items-start gap-3 rounded-lg border bg-popover px-4 py-3 text-sm text-popover-foreground shadow-raised animate-in fade-in-0 slide-in-from-bottom-2 [&_svg]:mt-0.5 [&_svg]:size-4 [&_svg]:shrink-0",
        toast.tone === "error" && "border-destructive/40",
      )}
    >
      {ICONS[toast.tone ?? "info"]}
      <div className="min-w-0 flex-1 space-y-1">
        <p className="font-medium">{toast.title}</p>
        {toast.body && <p className="text-muted-foreground">{toast.body}</p>}
        {toast.action && (
          <button
            type="button"
            className="font-medium text-primary underline-offset-4 hover:underline"
            onClick={() => {
              toast.action?.onClick();
              onClose();
            }}
          >
            {toast.action.label}
          </button>
        )}
      </div>
      <button
        type="button"
        onClick={onClose}
        aria-label={closeLabel}
        className="rounded-sm text-muted-foreground hover:text-foreground"
      >
        <X aria-hidden />
      </button>
    </div>
  );
}
