// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
import type * as React from "react";

import { cn } from "@/lib/utils";

/** A keyboard key hint. Always LTR: shortcuts are typed the same way in every locale. */
export function Kbd({ className, ...props }: React.ComponentProps<"kbd">) {
  return (
    <kbd
      dir="ltr"
      className={cn(
        "inline-flex h-5 min-w-5 items-center justify-center rounded-sm border border-border bg-surface-sunken px-1 font-mono text-[0.68rem] leading-none font-medium text-muted-foreground",
        className,
      )}
      {...props}
    />
  );
}
