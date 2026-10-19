// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
import { cva, type VariantProps } from "class-variance-authority";
import type * as React from "react";

import { cn } from "@/lib/utils";

/**
 * Small labels. Square-ish corners (a ledger tag, not a pill). Status variants pair a
 * soft tint with its text-strength ink, AA in both themes; `signal` is the saffron
 * "needs you" tag and should stay rare.
 */
const badgeVariants = cva(
  "inline-flex items-center gap-1 rounded-sm border px-1.5 py-0.5 text-xs font-medium whitespace-nowrap [&_svg]:size-3",
  {
    variants: {
      variant: {
        default: "border-transparent bg-secondary text-secondary-foreground",
        signal: "border-transparent bg-signal-soft text-signal-foreground dark:text-signal",
        outline: "text-muted-foreground",
        info: "border-transparent bg-info-soft text-info",
        success: "border-transparent bg-success-soft text-success-ink",
        warning: "border-transparent bg-warning-soft text-warning-ink",
        danger: "border-transparent bg-danger-soft text-danger-ink",
        ink: "border-transparent bg-foreground text-background",
      },
    },
    defaultVariants: { variant: "default" },
  },
);

export function Badge({
  className,
  variant,
  ...props
}: React.ComponentProps<"span"> & VariantProps<typeof badgeVariants>) {
  return <span className={cn(badgeVariants({ variant }), className)} {...props} />;
}
