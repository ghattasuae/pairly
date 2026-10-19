// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
import type * as React from "react";

import { cn } from "@/lib/utils";

/*
 * Small "ink and saffron" pieces the Pay screens share. They live here until the shared
 * set in src/components/ui has an equivalent; each one is marked as a candidate.
 */

export type Tone = "neutral" | "info" | "success" | "warning" | "danger";

/** Soft tint, hairline and ink for each status tone. Never saffron: that is for actions. */
export const TONE = {
  neutral: { box: "border-border bg-surface-sunken", ink: "text-foreground", icon: "text-muted-foreground", bar: "bg-muted-foreground" },
  info: { box: "border-info/30 bg-info-soft", ink: "text-foreground", icon: "text-info", bar: "bg-info" },
  success: { box: "border-success/35 bg-success-soft", ink: "text-success-ink", icon: "text-success-ink", bar: "bg-success" },
  warning: { box: "border-warning-ink/35 bg-warning-soft", ink: "text-warning-ink", icon: "text-warning-ink", bar: "bg-warning" },
  danger: { box: "border-destructive/45 bg-danger-soft", ink: "text-danger-ink", icon: "text-danger-ink", bar: "bg-destructive" },
} as const satisfies Record<Tone, { box: string; ink: string; icon: string; bar: string }>;

// candidate for src/components/ui
/** A filter chip: an ink outline on the quiet wash when current, a hairline otherwise. */
export function chipClass(active: boolean): string {
  return active
    ? "inline-flex h-8 items-center gap-1.5 rounded-md border border-foreground/70 bg-accent px-3 text-[0.8rem] font-medium text-foreground"
    : "inline-flex h-8 items-center gap-1.5 rounded-md border border-border bg-surface px-3 text-[0.8rem] text-foreground hover:border-foreground/35 hover:bg-accent";
}

// candidate for src/components/ui
/** A section tab with an ink underline when current (the Pay section's own tabs). */
export function tabClass(active: boolean): string {
  return cn(
    "inline-flex h-10 items-center border-b-2 px-1 text-sm whitespace-nowrap",
    active
      ? "border-foreground font-medium text-foreground"
      : "border-transparent text-muted-foreground hover:border-rule hover:text-foreground",
  );
}

// candidate for src/components/ui
/**
 * A boxed note in one status tone: an icon, an optional title and the body. The tone is
 * carried by the icon and the words as well as the tint, never by colour alone.
 */
export function Callout({
  tone,
  icon,
  title,
  children,
  className,
  role,
  ...rest
}: {
  tone: Tone;
  icon?: React.ReactNode;
  title?: React.ReactNode;
  children?: React.ReactNode;
  className?: string;
  role?: string;
} & Record<`data-${string}`, string | number | boolean | undefined>) {
  const t = TONE[tone];
  return (
    <div role={role} className={cn("flex items-start gap-3 rounded-lg border p-4 text-sm", t.box, className)} {...rest}>
      {icon && <span className={cn("mt-0.5 shrink-0 [&_svg]:size-5", t.icon)}>{icon}</span>}
      <div className="min-w-0 flex-1 space-y-1">
        {title && <p className={cn("font-medium", t.ink)}>{title}</p>}
        {children}
      </div>
    </div>
  );
}

// candidate for src/components/ui
/** A labelled big number: Fraunces, lining tabular figures, optional tone for the value. */
export function Figure({
  label,
  value,
  hint,
  tone,
  className,
  ...rest
}: {
  label: React.ReactNode;
  value: React.ReactNode;
  hint?: React.ReactNode;
  tone?: Tone;
  className?: string;
} & Record<`data-${string}`, string | number | boolean | undefined>) {
  return (
    <div className={cn("min-w-0 space-y-1", className)} {...rest}>
      <p className="eyebrow">{label}</p>
      <p className={cn("figure text-3xl leading-none", tone && tone !== "neutral" && TONE[tone].ink)} data-value>
        {value}
      </p>
      {hint && <p className="text-xs text-muted-foreground">{hint}</p>}
    </div>
  );
}

// candidate for src/components/ui
/**
 * How sure the reader was, as a thin meter next to the words. The tone follows the
 * review state (doubtful is warning, missing is danger, confident is success), so the
 * colour repeats what the text already says.
 */
export function ConfidenceMeter({ value, tone, children }: { value: number | null | undefined; tone: Tone; children: React.ReactNode }) {
  const pct = Math.max(0, Math.min(1, value ?? 0)) * 100;
  return (
    <span className="inline-flex items-center gap-1.5">
      <span aria-hidden className="inline-block h-1 w-10 overflow-hidden rounded-full bg-muted">
        <span className={cn("block h-full rounded-full", TONE[tone].bar)} style={{ inlineSize: `${pct}%` }} />
      </span>
      <span>{children}</span>
    </span>
  );
}

export type StepState = "done" | "current" | "blocked" | "todo";

const STEP_DOT: Record<StepState, string> = {
  done: "border-success bg-success text-background",
  current: "border-foreground bg-surface text-foreground",
  blocked: "border-destructive bg-danger-soft text-danger-ink",
  todo: "border-input bg-surface text-muted-foreground",
};

// candidate for src/components/ui
/**
 * A vertical list of steps with a hairline between them: done (filled), current (ink
 * ring), blocked (danger) and still to come (outline). The state is also given in the
 * `data-step-state` attribute and, for screen readers, by `aria-current` on the current
 * step; the visible words of each step say the rest.
 */
export function StepList({
  steps,
  className,
  label,
}: {
  steps: { key: string; state: StepState; title: React.ReactNode; body?: React.ReactNode; icon?: React.ReactNode }[];
  className?: string;
  label?: string;
}) {
  return (
    <ol className={cn("space-y-0", className)} aria-label={label}>
      {steps.map((step, i) => (
        <li
          key={step.key}
          className="relative flex gap-3 pb-4 last:pb-0"
          data-step-state={step.state}
          aria-current={step.state === "current" ? "step" : undefined}
        >
          {i < steps.length - 1 && (
            <span aria-hidden className="absolute start-[0.6875rem] top-6 bottom-0 w-px bg-rule" />
          )}
          <span
            aria-hidden
            className={cn(
              "relative z-[1] flex size-6 shrink-0 items-center justify-center rounded-full border-2 text-[0.65rem] font-semibold [&_svg]:size-3.5",
              STEP_DOT[step.state],
            )}
          >
            {step.icon ?? i + 1}
          </span>
          <div className="min-w-0 flex-1 space-y-0.5 pt-0.5 text-sm">
            <p className={cn("font-medium", step.state === "todo" && "text-muted-foreground")}>{step.title}</p>
            {step.body && <div className="text-muted-foreground">{step.body}</div>}
          </div>
        </li>
      ))}
    </ol>
  );
}

/** Classes for the Pay tables: a quiet header row on sunken paper, hairline rows. */
export const TABLE = {
  wrap: "overflow-x-auto rounded-lg border border-border bg-surface",
  table: "w-full text-sm",
  headRow: "border-b border-border bg-surface-sunken text-xs text-muted-foreground",
  th: "px-3 py-2 text-start font-medium",
  thEnd: "px-3 py-2 text-end font-medium",
  row: "border-b border-border last:border-0",
  td: "px-3 py-2 align-top",
  tdEnd: "px-3 py-2 text-end align-top font-mono tabular-nums",
} as const;
