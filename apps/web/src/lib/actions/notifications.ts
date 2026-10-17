// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
"use server";

/** Server actions for in-app notifications and the user's own notification preferences. */
import { z } from "zod";

import type { ActionResult } from "../action-result";
import { callApi, toAction } from "../api/call";
import type { Notification, Preferences } from "../api/types";

/** The newest in-app notifications and the unread count. */
export async function loadNotifications(): Promise<ActionResult<{ items: Notification[]; unread: number }>> {
  const result = await callApi((api) => api.GET("/v1/notifications", { params: { query: { limit: 20 } } }));
  return toAction(result, (data) => ({ items: data.items, unread: data.unread }));
}

export async function markNotificationRead(id: string): Promise<ActionResult> {
  if (!z.uuid().safeParse(id).success) return { ok: false, code: "invalid_input" };
  const result = await callApi((api) =>
    api.POST("/v1/notifications/{notification_id}/read", { params: { path: { notification_id: id } } }),
  );
  return toAction(result, () => ({}));
}

export async function markAllNotificationsRead(): Promise<ActionResult> {
  const result = await callApi((api) => api.POST("/v1/notifications/read-all"));
  return toAction(result, () => ({}));
}

const channel = z.enum(["email", "slack", "in_app"]);
const category = z.string().regex(/^[a-z][a-z0-9_]{0,63}$/);

const preferencesSchema = z
  .object({
    locale: z.enum(["en", "ar"]),
    timezone: z.string().min(1).max(64),
    channels: z.record(category, z.array(channel).max(3)),
    digest: z.record(category, z.enum(["realtime", "daily"])),
    snooze_until: z.iso.datetime({ offset: true }).nullable(),
    quiet_start: z.number().int().min(0).max(1439).nullable(),
    quiet_end: z.number().int().min(0).max(1439).nullable(),
  })
  .refine((p) => (p.quiet_start === null) === (p.quiet_end === null), { message: "quiet hours need both ends" });

export type PreferencesForm = z.infer<typeof preferencesSchema>;

/** Save the user's preferences. The API validates categories and time zones again. */
export async function savePreferences(input: PreferencesForm): Promise<ActionResult<{ preferences: Preferences }>> {
  const parsed = preferencesSchema.safeParse(input);
  if (!parsed.success) return { ok: false, code: "invalid_input" };
  const result = await callApi((api) => api.PUT("/v1/notifications/preferences", { body: parsed.data }));
  return toAction(result, (preferences) => ({ preferences }));
}
