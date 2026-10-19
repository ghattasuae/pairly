// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
"use client";

import { Dialog as DialogPrimitive } from "radix-ui";
import type * as React from "react";

import { cn } from "@/lib/utils";

export const Dialog = DialogPrimitive.Root;
export const DialogTitle = DialogPrimitive.Title;
export const DialogDescription = DialogPrimitive.Description;

export function DialogContent({
  className,
  children,
  ...props
}: React.ComponentProps<typeof DialogPrimitive.Content>) {
  return (
    <DialogPrimitive.Portal>
      <DialogPrimitive.Overlay className="fixed inset-0 z-50 bg-foreground/25 backdrop-blur-[2px] animate-in fade-in-0" />
      <DialogPrimitive.Content
        className={cn(
          "fixed start-1/2 top-[14vh] z-50 w-[calc(100%-2rem)] max-w-xl -translate-x-1/2 rounded-xl border bg-popover text-popover-foreground shadow-raised outline-none animate-in fade-in-0 zoom-in-95 rtl:translate-x-1/2",
          className,
        )}
        {...props}
      >
        {children}
      </DialogPrimitive.Content>
    </DialogPrimitive.Portal>
  );
}

/** The head of a dialog or sheet: a Fraunces title and one muted line under it. */
export function DialogHeader({ className, ...props }: React.ComponentProps<"div">) {
  return <div className={cn("space-y-1.5 border-b border-border px-6 pt-6 pb-4", className)} {...props} />;
}

/** The scrolling middle of a dialog or sheet. */
export function DialogBody({ className, ...props }: React.ComponentProps<"div">) {
  return <div className={cn("space-y-4 px-6 py-5", className)} {...props} />;
}

/** The actions row: the primary action on the end side, stacked on phones. */
export function DialogFooter({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      className={cn(
        "flex flex-col-reverse gap-2 border-t border-border bg-surface-sunken px-6 py-4 sm:flex-row sm:justify-end",
        className,
      )}
      {...props}
    />
  );
}

/** The title style shared by dialogs and sheets. */
export const dialogTitleClass = "font-display text-2xl leading-tight";

/**
 * A sheet: a full-height panel that slides in from the end side (right in English, left
 * in Arabic) for details that keep the list behind them in view. Same parts as a dialog
 * (`DialogHeader`, `DialogBody`, `DialogFooter`). The slide is skipped under reduced
 * motion by the global rule.
 */
export function SheetContent({
  className,
  children,
  ...props
}: React.ComponentProps<typeof DialogPrimitive.Content>) {
  return (
    <DialogPrimitive.Portal>
      <DialogPrimitive.Overlay className="fixed inset-0 z-50 bg-foreground/25 animate-in fade-in-0" />
      <DialogPrimitive.Content
        className={cn(
          "fixed inset-y-0 end-0 z-50 flex w-full max-w-md flex-col overflow-y-auto border-s border-border bg-popover text-popover-foreground shadow-raised outline-none animate-in slide-in-from-end-8",
          className,
        )}
        {...props}
      >
        {children}
      </DialogPrimitive.Content>
    </DialogPrimitive.Portal>
  );
}
