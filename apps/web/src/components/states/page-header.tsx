// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
import type * as React from "react";

import { cn } from "@/lib/utils";

/**
 * The asymmetric page header every signed-in page opens with: a small eyebrow, the title
 * in Fraunces on the start side, the page's actions on the end side, and one line of
 * context below in muted ink. A hairline rule closes it, like the head of a ledger page.
 */
export function PageHeader({
  eyebrow,
  title,
  actions,
  children,
  className,
}: {
  eyebrow?: React.ReactNode;
  title: React.ReactNode;
  /** Buttons or links for this page, shown on the end side (below the title on phones). */
  actions?: React.ReactNode;
  /** One line of context under the title. */
  children?: React.ReactNode;
  className?: string;
}) {
  return (
    <header
      className={cn(
        "rise flex flex-wrap items-end justify-between gap-x-10 gap-y-4 border-b border-border pb-6",
        className,
      )}
      style={{ "--rise-index": 0 } as React.CSSProperties}
    >
      <div className="min-w-0 max-w-3xl space-y-2">
        {eyebrow && <p className="eyebrow">{eyebrow}</p>}
        <h1 className="font-display text-[2rem] leading-[1.1] font-normal text-balance sm:text-[2.6rem]">{title}</h1>
        {children && <div className="text-muted-foreground">{children}</div>}
      </div>
      {actions && <div className="flex shrink-0 flex-wrap items-center gap-2">{actions}</div>}
    </header>
  );
}
