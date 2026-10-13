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
import type { HireInterviewPlan, HireInterviewPlanView } from "../hire/types";

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
