// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
"use server";

/**
 * Server actions for interviews (M3, brief 3.7): the interview plan, kits, scorecards and
 * the debrief decision, for signed-in people. Each one checks its input shape and calls
 * the API with the user's token; the API decides everything (who may see whose
 * scorecard, blind mode, second factor for decisions).
 */
import { z } from "zod";

import type { ActionResult } from "../action-result";
import { callApi, toAction } from "../api/call";
import type { HireDebrief, HireInterviewPlan, HireInterviewPlanView } from "../hire/types";

const uuid = z.uuid();
const bad = { ok: false as const, code: "invalid_input" };

export async function saveInterviewPlan(
  jobId: string,
  plan: HireInterviewPlan,
  expectedVersion: number | null,
): Promise<ActionResult<{ view: HireInterviewPlanView }>> {
  if (!uuid.safeParse(jobId).success) return bad;
  if (expectedVersion !== null && !Number.isInteger(expectedVersion)) return bad;
  const result = await callApi((api) =>
    api.PUT("/v1/hire/jobs/{job_id}/interview-plan", {
      params: { path: { job_id: jobId } },
      body: { plan: plan as unknown as Record<string, unknown>, expected_version: expectedVersion },
    }),
  );
  return toAction(result, (view) => ({ view }));
}

const stageKey = z.string().regex(/^[a-z][a-z0-9_]{0,31}$/);

/** The hiring manager's decision on a debrief: advance, hold or reject, with a reason. */
export async function decideDebrief(input: {
  applicationId: string;
  stage: string;
  decision: "advance" | "hold" | "reject";
  reason: string;
}): Promise<ActionResult<{ debrief: HireDebrief }>> {
  const parsed = z
    .object({
      applicationId: uuid,
      stage: stageKey,
      decision: z.enum(["advance", "hold", "reject"]),
      reason: z.string().trim().min(3).max(1000),
    })
    .safeParse(input);
  if (!parsed.success) return bad;
  const result = await callApi((api) =>
    api.POST("/v1/hire/applications/{application_id}/debrief/decision", {
      params: { path: { application_id: parsed.data.applicationId } },
      body: { stage: parsed.data.stage, decision: parsed.data.decision, reason: parsed.data.reason },
    }),
  );
  return toAction(result, (debrief) => ({ debrief }));
}
