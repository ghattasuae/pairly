// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
"use client";

import { registerSchedulePanel, type SchedulePanelProps } from "../schedule-slot";

import { SchedulingPanel } from "./scheduling-panel";

/**
 * Screening mode's "S: schedule" (WP2.6) opens the real scheduling panel (WP2.8). The
 * panel loads its own, blind-safe view of the application; after a booking or a sent
 * link, screening mode reloads the candidate.
 */
export function ScreeningSchedulePanel({ applicationId, onClose, onDone }: SchedulePanelProps) {
  return <SchedulingPanel key={applicationId} applicationId={applicationId} onClose={onClose} onChanged={() => onDone()} />;
}

registerSchedulePanel(ScreeningSchedulePanel);
