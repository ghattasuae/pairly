// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
"use client";

import { createElement } from "react";
import type * as React from "react";

/**
 * Where screening mode's "S: schedule" opens scheduling (WP2.8). Scheduling registers its
 * panel here (`registerSchedulePanel`); until it does, and until the API says scheduling
 * is available (`scheduling_available` on the queue), the action stays disabled and says
 * why. Screening mode never depends on how scheduling works.
 */
export interface SchedulePanelProps {
  applicationId: string;
  /** The candidate as the reviewer may see them (the blind label while blind). */
  label: string;
  onClose: () => void;
  /** After something was booked or sent: the screen reloads the candidate. */
  onDone: () => void;
}

let registered: React.ComponentType<SchedulePanelProps> | null = null;

export function registerSchedulePanel(panel: React.ComponentType<SchedulePanelProps> | null): void {
  registered = panel;
}

export function schedulePanel(): React.ComponentType<SchedulePanelProps> | null {
  return registered;
}

/** The registered scheduling panel, or nothing. */
export function ScheduleSlot(props: SchedulePanelProps): React.ReactNode {
  return registered ? createElement(registered, props) : null;
}
