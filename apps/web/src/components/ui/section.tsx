// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
import type * as React from "react";

import { cn } from "@/lib/utils";

/**
 * A hairline section card: a small heading row (title on the start side, a count, link
 * or quiet action on the end side), an optional muted line, then the content. No shadow.
 * Use `flush` when the content is a list or table that runs edge to edge.
 */
export function Section({
  title,
  description,
  actions,
  headingLevel = 2,
  flush = false,
  className,
  children,
  ...props
}: Omit<React.ComponentProps<"section">, "title"> & {
  title?: React.ReactNode;
  description?: React.ReactNode;
  actions?: React.ReactNode;
  headingLevel?: 2 | 3;
  flush?: boolean;
}) {
  const Heading = headingLevel === 3 ? "h3" : "h2";
  const head = (title || actions) && (
    <div className={cn("card-head", flush ? "border-b border-border px-5 py-3.5" : "")}>
      {title && <Heading className="font-medium">{title}</Heading>}
      {actions && <div className="flex flex-wrap items-center gap-2 text-sm">{actions}</div>}
    </div>
  );
  return (
    <section className={cn("card-hairline", flush ? "overflow-hidden" : "space-y-3 p-5", className)} {...props}>
      {head}
      {description && (
        <p className={cn("text-sm text-muted-foreground", flush && "px-5 pt-3")}>{description}</p>
      )}
      {children}
    </section>
  );
}
