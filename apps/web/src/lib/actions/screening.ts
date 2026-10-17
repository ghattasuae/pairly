// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
"use server";

/**
 * Server actions for recruiter screening mode (WP2.6). Each one checks its input shape and
 * calls the API as the signed-in user. The API decides everything (permissions, second
 * factor, blind mode); nothing here does. Moves, notes, tags, bulk actions and messages
 * reuse the pipeline actions (`./hire`).
 */
import { z } from "zod";

import type { ActionResult } from "../action-result";
import { callApi, toAction } from "../api/call";
import type { CandidateDetail, ScreeningPanel, ScreeningQueue } from "../hire/screening";

const uuid = z.uuid();
const bad = { ok: false as const, code: "invalid_input" };

export async function loadQueue(jobId: string): Promise<ActionResult<{ queue: ScreeningQueue }>> {
  if (!uuid.safeParse(jobId).success) return bad;
  const result = await callApi((api) =>
    api.GET("/v1/hire/jobs/{job_id}/screening-queue", { params: { path: { job_id: jobId } } }),
  );
  return toAction(result, (queue) => ({ queue }));
}

/** The explainability panel and the profile of one candidate, together. */
export async function loadCandidate(applicationId: string): Promise<ActionResult<{ detail: CandidateDetail }>> {
  if (!uuid.safeParse(applicationId).success) return bad;
  const [screening, profile] = await Promise.all([
    callApi((api) =>
      api.GET("/v1/hire/applications/{application_id}/screening", {
        params: { path: { application_id: applicationId } },
      }),
    ),
    callApi((api) =>
      api.GET("/v1/hire/pipeline/applications/{application_id}", {
        params: { path: { application_id: applicationId } },
      }),
    ),
  ]);
  if (!profile.ok) return { ok: false, code: profile.code };
  if (!screening.ok && screening.code !== "not_found") return { ok: false, code: screening.code };
  return { ok: true, detail: { screening: screening.ok ? screening.data : null, profile: profile.data } };
}

export async function overrideScores(input: {
  applicationId: string;
  scores: Record<string, number>;
  reason: string;
}): Promise<ActionResult<{ screening: ScreeningPanel }>> {
  const parsed = z
    .object({
      applicationId: uuid,
      scores: z
        .record(z.string().regex(/^[a-z][a-z0-9_]{0,47}$/), z.number().int().min(0).max(4))
        .refine((v) => Object.keys(v).length >= 1 && Object.keys(v).length <= 24),
      reason: z.string().trim().min(10).max(1000),
    })
    .safeParse(input);
  if (!parsed.success) return bad;
  const result = await callApi((api) =>
    api.POST("/v1/hire/applications/{application_id}/screening/overrides", {
      params: { path: { application_id: parsed.data.applicationId } },
      body: { scores: parsed.data.scores, reason: parsed.data.reason },
    }),
  );
  return toAction(result, (screening) => ({ screening }));
}

export async function setBlindMode(enabled: boolean): Promise<ActionResult<{ blindMode: boolean }>> {
  if (typeof enabled !== "boolean") return bad;
  const result = await callApi((api) =>
    api.PUT("/v1/hire/screening/settings", { body: { blind_mode: enabled } }),
  );
  return toAction(result, (data) => ({ blindMode: data.blind_mode }));
}
