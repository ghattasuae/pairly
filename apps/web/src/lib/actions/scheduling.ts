// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
"use server";

/**
 * Server actions for interview scheduling (WP2.8). Each one checks its input shape and
 * calls the API with the signed-in user's token; the API decides everything (permissions,
 * panel membership, conflicts, blind mode). Writes a retry could repeat carry an
 * Idempotency-Key chosen by the browser.
 */
import { z } from "zod";

import type { ActionResult } from "../action-result";
import { callApi, toAction } from "../api/call";
import type {
  Availability,
  CalendarIntegrations,
  Interview,
  SchedulingPanelData,
  SlotCheck,
  SlotsResult,
} from "../hire/scheduling";

const uuid = z.uuid();
const key = z.string().regex(/^[A-Za-z0-9._:-]{8,128}$/);
const panel = z.array(uuid).min(1).max(6);
const duration = z.number().int().min(15).max(240);
const tz = z.string().min(1).max(64).regex(/^[A-Za-z0-9_+\-/]+$/);
const iso = z.iso.datetime({ offset: true });
const meetingUrl = z.string().max(900).regex(/^https:\/\/\S+$/);

const bad = { ok: false as const, code: "invalid_input" };

export async function loadScheduling(applicationId: string): Promise<ActionResult<{ data: SchedulingPanelData }>> {
  if (!uuid.safeParse(applicationId).success) return bad;
  const result = await callApi((api) =>
    api.GET("/v1/hire/scheduling/applications/{application_id}", {
      params: { path: { application_id: applicationId } },
    }),
  );
  return toAction(result, (data) => ({ data }));
}

export async function findSlots(input: {
  applicationId: string;
  panel: string[];
  durationMin: number;
  candidateTimezone?: string;
  interviewId?: string | null;
  start?: string | null;
  days?: number;
}): Promise<ActionResult<{ slots: SlotsResult }>> {
  const parsed = z
    .object({
      applicationId: uuid,
      panel,
      durationMin: duration,
      candidateTimezone: tz.optional(),
      interviewId: uuid.nullable().optional(),
      start: iso.nullable().optional(),
      days: z.number().int().min(1).max(60).optional(),
    })
    .safeParse(input);
  if (!parsed.success) return bad;
  const p = parsed.data;
  const result = await callApi((api) =>
    api.POST("/v1/hire/scheduling/applications/{application_id}/slots", {
      params: { path: { application_id: p.applicationId } },
      body: {
        panel: p.panel,
        duration_min: p.durationMin,
        candidate_timezone: p.candidateTimezone ?? null,
        interview_id: p.interviewId ?? null,
        start: p.start ?? null,
        days: p.days ?? null,
      },
    }),
  );
  return toAction(result, (slots) => ({ slots }));
}

export async function checkSlot(input: {
  applicationId: string;
  panel: string[];
  startsAt: string;
  durationMin: number;
  interviewId?: string | null;
}): Promise<ActionResult<{ check: SlotCheck }>> {
  const parsed = z
    .object({ applicationId: uuid, panel, startsAt: iso, durationMin: duration, interviewId: uuid.nullable().optional() })
    .safeParse(input);
  if (!parsed.success) return bad;
  const p = parsed.data;
  const result = await callApi((api) =>
    api.POST("/v1/hire/scheduling/applications/{application_id}/check", {
      params: { path: { application_id: p.applicationId } },
      body: { panel: p.panel, starts_at: p.startsAt, duration_min: p.durationMin, interview_id: p.interviewId ?? null },
    }),
  );
  return toAction(result, (check) => ({ check }));
}

export async function createInterview(input: {
  applicationId: string;
  mode: "book" | "link";
  panel: string[];
  durationMin: number;
  startsAt?: string | null;
  candidateTimezone?: string | null;
  meetingUrl?: string | null;
  force?: boolean;
  idempotencyKey: string;
}): Promise<ActionResult<{ interview: Interview }>> {
  const parsed = z
    .object({
      applicationId: uuid,
      mode: z.enum(["book", "link"]),
      panel,
      durationMin: duration,
      startsAt: iso.nullable().optional(),
      candidateTimezone: tz.nullable().optional(),
      meetingUrl: meetingUrl.nullable().optional(),
      force: z.boolean().optional(),
      idempotencyKey: key,
    })
    .safeParse(input);
  if (!parsed.success) return bad;
  const p = parsed.data;
  const result = await callApi(
    (api) =>
      api.POST("/v1/hire/scheduling/applications/{application_id}/interviews", {
        params: { path: { application_id: p.applicationId } },
        body: {
          mode: p.mode,
          panel: p.panel,
          duration_min: p.durationMin,
          starts_at: p.startsAt ?? null,
          candidate_timezone: p.candidateTimezone ?? null,
          meeting_url: p.meetingUrl ?? null,
          force: p.force ?? false,
        },
        headers: { "Idempotency-Key": p.idempotencyKey },
      }),
    { okStatuses: [201] },
  );
  return toAction(result, (interview) => ({ interview }));
}

export async function updateInterview(
  interviewId: string,
  patch: {
    startsAt?: string;
    durationMin?: number;
    panel?: string[];
    meetingUrl?: string;
    clearMeetingUrl?: boolean;
    candidateTimezone?: string;
    force?: boolean;
  },
): Promise<ActionResult<{ interview: Interview }>> {
  const parsed = z
    .object({
      interviewId: uuid,
      startsAt: iso.optional(),
      durationMin: duration.optional(),
      panel: panel.optional(),
      meetingUrl: meetingUrl.optional(),
      clearMeetingUrl: z.boolean().optional(),
      candidateTimezone: tz.optional(),
      force: z.boolean().optional(),
    })
    .safeParse({ interviewId, ...patch });
  if (!parsed.success) return bad;
  const p = parsed.data;
  const result = await callApi((api) =>
    api.PATCH("/v1/hire/scheduling/interviews/{interview_id}", {
      params: { path: { interview_id: p.interviewId } },
      body: {
        starts_at: p.startsAt ?? null,
        duration_min: p.durationMin ?? null,
        panel: p.panel ?? null,
        meeting_url: p.meetingUrl ?? null,
        clear_meeting_url: p.clearMeetingUrl ?? false,
        candidate_timezone: p.candidateTimezone ?? null,
        force: p.force ?? false,
      },
    }),
  );
  return toAction(result, (interview) => ({ interview }));
}

export async function cancelInterview(
  interviewId: string,
  idempotencyKey: string,
): Promise<ActionResult<{ interview: Interview }>> {
  if (!uuid.safeParse(interviewId).success || !key.safeParse(idempotencyKey).success) return bad;
  const result = await callApi((api) =>
    api.POST("/v1/hire/scheduling/interviews/{interview_id}/cancel", {
      params: { path: { interview_id: interviewId } },
      headers: { "Idempotency-Key": idempotencyKey },
    }),
  );
  return toAction(result, (interview) => ({ interview }));
}

export async function resendLink(
  interviewId: string,
  idempotencyKey: string,
): Promise<ActionResult<{ interview: Interview }>> {
  if (!uuid.safeParse(interviewId).success || !key.safeParse(idempotencyKey).success) return bad;
  const result = await callApi((api) =>
    api.POST("/v1/hire/scheduling/interviews/{interview_id}/link", {
      params: { path: { interview_id: interviewId } },
      headers: { "Idempotency-Key": idempotencyKey },
    }),
  );
  return toAction(result, (interview) => ({ interview }));
}

export async function recordOutcome(
  interviewId: string,
  outcome: "completed" | "no_show",
): Promise<ActionResult<{ interview: Interview }>> {
  if (!uuid.safeParse(interviewId).success || !["completed", "no_show"].includes(outcome)) return bad;
  const result = await callApi((api) =>
    api.POST("/v1/hire/scheduling/interviews/{interview_id}/outcome", {
      params: { path: { interview_id: interviewId } },
      body: { outcome },
    }),
  );
  return toAction(result, (interview) => ({ interview }));
}

/** The job's upcoming interviews (the board's interview chips). */
export async function jobInterviews(jobId: string): Promise<ActionResult<{ interviews: Interview[] }>> {
  if (!uuid.safeParse(jobId).success) return bad;
  const result = await callApi((api) =>
    api.GET("/v1/hire/scheduling/interviews", { params: { query: { job_id: jobId, upcoming: true } } }),
  );
  return toAction(result, (interviews) => ({ interviews }));
}

// ------------------------------------------------------------------ settings page

export async function loadIntegrations(): Promise<ActionResult<{ integrations: CalendarIntegrations }>> {
  const result = await callApi((api) => api.GET("/v1/hire/scheduling/integrations"));
  return toAction(result, (integrations) => ({ integrations }));
}

export async function startGoogle(): Promise<ActionResult<{ url: string }>> {
  const result = await callApi((api) => api.POST("/v1/hire/scheduling/integrations/google/start"));
  return toAction(result, (data) => ({ url: data.url }));
}

export async function finishGoogle(code: string, state: string): Promise<ActionResult<{ integrations: CalendarIntegrations }>> {
  const parsed = z.object({ code: z.string().min(1).max(512), state: z.string().min(1).max(300) }).safeParse({ code, state });
  if (!parsed.success) return bad;
  const result = await callApi((api) =>
    api.POST("/v1/hire/scheduling/integrations/google/callback", { body: parsed.data }),
  );
  return toAction(result, (integrations) => ({ integrations }));
}

export async function disconnectGoogle(): Promise<ActionResult<{ integrations: CalendarIntegrations }>> {
  const result = await callApi((api) => api.POST("/v1/hire/scheduling/integrations/google/disconnect"));
  return toAction(result, (integrations) => ({ integrations }));
}

export async function myAvailability(): Promise<ActionResult<{ availability: Availability }>> {
  const result = await callApi((api) => api.GET("/v1/hire/scheduling/availability/me"));
  return toAction(result, (availability) => ({ availability }));
}

const hhmm = z.string().regex(/^([01]\d|2[0-3]):[0-5]\d$|^24:00$/);

export async function saveMyAvailability(input: {
  timezone: string;
  workingHours: Record<string, [string, string][]>;
  bufferBeforeMin: number;
  bufferAfterMin: number;
  maxPerDay: number | null;
  holidays: string[];
}): Promise<ActionResult<{ availability: Availability }>> {
  const parsed = z
    .object({
      timezone: tz,
      workingHours: z.record(
        z.enum(["mon", "tue", "wed", "thu", "fri", "sat", "sun"]),
        z.array(z.tuple([hhmm, hhmm])).max(4),
      ),
      bufferBeforeMin: z.number().int().min(0).max(240),
      bufferAfterMin: z.number().int().min(0).max(240),
      maxPerDay: z.number().int().min(0).max(24).nullable(),
      holidays: z.array(z.string().regex(/^\d{4}-\d{2}-\d{2}$/)).max(366),
    })
    .safeParse(input);
  if (!parsed.success) return bad;
  const p = parsed.data;
  const result = await callApi((api) =>
    api.PUT("/v1/hire/scheduling/availability/me", {
      body: {
        timezone: p.timezone,
        working_hours: p.workingHours,
        buffer_before_min: p.bufferBeforeMin,
        buffer_after_min: p.bufferAfterMin,
        max_per_day: p.maxPerDay,
        holidays: p.holidays,
      },
    }),
  );
  return toAction(result, (availability) => ({ availability }));
}
