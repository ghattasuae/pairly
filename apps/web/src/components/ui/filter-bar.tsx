// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
import type * as React from "react";

import { cn } from "@/lib/utils";

import { buttonVariants } from "./button";

/** The class for a compact field inside a filter bar (native select or input). */
export const filterFieldClass =
  "h-9 rounded-md border border-input bg-surface px-2.5 text-sm text-foreground outline-none focus-visible:border-ring focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-solid focus-visible:outline-ring";

/** The class for the submit button that applies a filter bar (outline, never saffron). */
export const filterApplyClass = buttonVariants({ variant: "outline", size: "sm", className: "h-9" });

/**
 * A ruled strip of filters above a list or a report. Usually a plain GET form, so the
 * view lives in the URL and can be shared and reloaded. Put the apply button last.
 */
export function FilterBar({ className, ...props }: React.ComponentProps<"form">) {
  return <form className={cn("filter-bar", className)} {...props} />;
}

/** One labelled field in a filter bar: the label sits above the control. */
export function FilterField({
  label,
  className,
  children,
}: {
  label: React.ReactNode;
  className?: string;
  children: React.ReactNode;
}) {
  return (
    <label className={cn("flex flex-col gap-1 text-xs font-medium text-muted-foreground", className)}>
      {label}
      {children}
    </label>
  );
}
