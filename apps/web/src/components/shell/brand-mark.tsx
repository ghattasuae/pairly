// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
import { cn } from "@/lib/utils";

/**
 * The Pairly mark: two rings that overlap, drawn in ink. One is the AI, one is the person.
 * The shared lens in the middle, in saffron, is where decisions happen.
 */
export function BrandMark({ className, title }: { className?: string; title?: string }) {
  return (
    <svg
      viewBox="0 0 32 32"
      className={cn("size-7", className)}
      role={title ? "img" : undefined}
      aria-hidden={title ? undefined : true}
      aria-label={title}
    >
      <circle cx="12" cy="16" r="8.5" fill="none" stroke="currentColor" strokeWidth="2.2" opacity="0.55" />
      <circle cx="20" cy="16" r="8.5" fill="none" stroke="currentColor" strokeWidth="2.2" />
      <path d="M16 9.1a8.5 8.5 0 0 1 0 13.8a8.5 8.5 0 0 1 0-13.8z" fill="var(--signal)" />
    </svg>
  );
}

export function Wordmark({ className }: { className?: string }) {
  return (
    <span dir="ltr" className={cn("inline-flex items-center gap-2 text-foreground", className)}>
      <BrandMark />
      <span className="font-display text-xl font-medium tracking-tight text-foreground">Pairly</span>
    </span>
  );
}
