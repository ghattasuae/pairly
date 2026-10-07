// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
"use client";

import { AlarmClock, CircleCheck, CircleDot, CircleSlash, CircleX, Clock, RotateCcw } from "lucide-react";
import { useTranslations } from "next-intl";

import { Badge } from "@/components/ui/badge";
import type { SlaState } from "@/lib/api/types";
import { cn } from "@/lib/utils";

/**
 * SLA state of the active step. Colour is never the only signal: each state has its own
 * icon and word. Overdue uses the destructive ink, due soon the amber "needs you" signal.
 */
export function SlaBadge({ state, className }: { state: SlaState | null | undefined; className?: string }) {
  const t = useTranslations("Sla");
  if (!state) return null;
  const styles: Record<SlaState, string> = {
    overdue: "border-destructive/40 bg-destructive/10 text-destructive",
    due_soon: "border-transparent bg-signal-soft text-signal-foreground dark:text-signal",
    on_track: "text-muted-foreground",
  };
  const Icon = state === "overdue" ? AlarmClock : state === "due_soon" ? Clock : CircleDot;
  return (
    <Badge variant="outline" className={cn(styles[state], "[&_svg]:size-3", className)}>
      <Icon aria-hidden />
      {t(state)}
    </Badge>
  );
}

const STATUS_ICONS = {
  pending: CircleDot,
  approved: CircleCheck,
  rejected: CircleX,
  changes_requested: RotateCcw,
  cancelled: CircleSlash,
} as const;

/** Approval status in words, with an icon. Unknown statuses show as they are. */
export function ApprovalStatusBadge({ status, className }: { status: string; className?: string }) {
  const t = useTranslations("ApprovalStatus");
  const known = status in STATUS_ICONS;
  const Icon = known ? STATUS_ICONS[status as keyof typeof STATUS_ICONS] : CircleDot;
  return (
    <Badge
      variant="outline"
      className={cn(
        "[&_svg]:size-3",
        status === "approved" && "border-success/40 text-success",
        status === "rejected" && "border-destructive/40 text-destructive",
        status === "changes_requested" && "border-signal/50",
        className,
      )}
    >
      <Icon aria-hidden />
      {known ? t(status as keyof typeof STATUS_ICONS) : status}
    </Badge>
  );
}
