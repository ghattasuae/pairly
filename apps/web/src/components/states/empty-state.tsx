// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
import type * as React from "react";

import { cn } from "@/lib/utils";

/** Quiet empty state: an icon in a ruled tile, one line of title, one of help. */
export function EmptyState({
  icon,
  title,
  body,
  action,
  className,
}: {
  icon?: React.ReactNode;
  title: React.ReactNode;
  body?: React.ReactNode;
  action?: React.ReactNode;
  className?: string;
}) {
  return (
    <div className={cn("flex flex-col items-start gap-3 py-8", className)}>
      {icon && (
        <div className="flex size-10 items-center justify-center rounded-lg border bg-surface text-primary [&_svg]:size-5">
          {icon}
        </div>
      )}
      <div className="space-y-1">
        <p className="font-display text-xl leading-snug">{title}</p>
        {body && <p className="max-w-prose text-sm text-muted-foreground">{body}</p>}
      </div>
      {action}
    </div>
  );
}
