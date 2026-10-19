// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
import type * as React from "react";

import { cn } from "@/lib/utils";

/**
 * Link tabs in the ledger style (each tab is its own URL): plain ink labels on a
 * hairline, the current one carries a saffron underline (signal-ink, 3:1 or more as a non-text mark) and medium weight, so it never
 * relies on colour alone. Mark the current link with `aria-current="page"`. Server safe;
 * for panels inside one page use `Tabs` from `./tabs`.
 */
export const tabListClass = "flex flex-wrap items-end gap-x-5 gap-y-1 border-b border-border";

export const tabBaseClass =
  "-mb-px inline-flex min-h-10 items-center gap-1.5 border-b-2 border-transparent px-0.5 text-sm text-muted-foreground hover:text-foreground [&_svg]:size-4";

/** The class for one link tab; pass whether it is the current page. */
export function tabClass(current: boolean, className?: string): string {
  return cn(tabBaseClass, current && "border-signal-ink font-medium text-foreground", className);
}

/** A row of link tabs. Give it an accessible name with `aria-label`. */
export function TabNav({ className, ...props }: React.ComponentProps<"nav">) {
  return <nav className={cn(tabListClass, className)} {...props} />;
}
