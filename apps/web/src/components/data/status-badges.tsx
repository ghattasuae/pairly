// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
"use client";

import { AlarmClock, CircleCheck, CircleDot, CircleSlash, CircleX, Clock, RotateCcw } from "lucide-react";
import { useTranslations } from "next-intl";

import { Badge } from "@/components/ui/badge";
import type { SlaState } from "@/lib/api/types";
import { cn } from "@/lib/utils";

/**
 * SLA state of the active step. Colour is never the only signal: each state has its own
 * icon and word. Overdue uses the danger tint and ink, due soon the warning tint and ink,
 * on track stays a quiet outline.
 */
export function SlaBadge({ state, className }: { state: SlaState | null | undefined; className?: string }) {
  const t = useTranslations("Sla");
  if (!state) return null;
  const variants = { overdue: "danger", due_soon: "warning", on_track: "outline" } as const;
  const Icon = state === "overdue" ? AlarmClock : state === "due_soon" ? Clock : CircleDot;
  return (
    <Badge variant={variants[state]} className={cn("[&_svg]:size-3", className)}>
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
      variant={
        status === "approved" ? "success" : status === "rejected" ? "danger" : status === "changes_requested" ? "warning" : "outline"
      }
      className={cn("[&_svg]:size-3", className)}
    >
      <Icon aria-hidden />
      {known ? t(status as keyof typeof STATUS_ICONS) : status}
    </Badge>
  );
}
