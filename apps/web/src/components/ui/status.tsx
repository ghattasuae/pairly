// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
import type * as React from "react";

import { cn } from "@/lib/utils";

/*
 * Status patterns in the "ink and saffron" style: one tone map (soft tint, hairline, ink,
 * icon and bar colour per status), a boxed callout, a labelled big number, a confidence
 * meter and a vertical step list. Tones are semantic only; saffron stays for actions.
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
