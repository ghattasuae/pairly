// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology

/** A filter chip: an ink outline on the quiet wash when current, a hairline otherwise. */
export function chipClass(active: boolean): string {
  return active
    ? "inline-flex h-8 items-center gap-1.5 rounded-md border border-foreground/70 bg-accent px-3 text-[0.8rem] font-medium text-foreground"
    : "inline-flex h-8 items-center gap-1.5 rounded-md border border-border bg-surface px-3 text-[0.8rem] text-foreground hover:border-foreground/35 hover:bg-accent";
}
