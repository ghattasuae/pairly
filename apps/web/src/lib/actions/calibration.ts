// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
"use server";

/**
 * Server actions of calibration mode (M3, brief 3.3.11): open an application to label,
 * save a person's scores per criterion (0 to 4), and ask for a model suggestion for a
 * low-agreement criterion. The API checks every permission; the AI's scores are never
 * sent to the person labelling.
 */
import { z } from "zod";

import type { ActionResult } from "../action-result";
import { callApi, toAction } from "../api/call";
import type { HireCalibrationApplication, HireCalibrationSuggestion } from "../hire/types";

const uuid = z.uuid();
const criterionId = z.string().regex(/^[a-z][a-z0-9_]{0,47}$/);
const scoresInput = z.record(criterionId, z.number().int().min(0).max(4));
const bad = { ok: false as const, code: "invalid_input" };

export async function loadCalibrationItem(
  applicationId: string,
): Promise<ActionResult<{ item: HireCalibrationApplication }>> {
  if (!uuid.safeParse(applicationId).success) return bad;
  const result = await callApi((api) =>
    api.GET("/v1/hire/calibration/items/{application_id}", {
      params: { path: { application_id: applicationId } },
    }),
  );
  return toAction(result, (item) => ({ item }));
}

export async function saveCalibrationLabels(
  applicationId: string,
  scores: Record<string, number>,
): Promise<ActionResult<{ item: HireCalibrationApplication }>> {
  const parsed = scoresInput.safeParse(scores);
  if (!uuid.safeParse(applicationId).success || !parsed.success) return bad;
  if (Object.keys(parsed.data).length === 0) return bad;
  const result = await callApi((api) =>
    api.PUT("/v1/hire/calibration/items/{application_id}", {
      params: { path: { application_id: applicationId } },
      body: { scores: parsed.data },
    }),
  );
  return toAction(result, (item) => ({ item }));
}

export async function suggestClarification(
  jobId: string,
  criterion: string,
): Promise<ActionResult<{ suggestion: HireCalibrationSuggestion }>> {
  if (!uuid.safeParse(jobId).success || !criterionId.safeParse(criterion).success) return bad;
  const result = await callApi((api) =>
    api.POST("/v1/hire/calibration/suggest", { body: { job_id: jobId, criterion_id: criterion } }),
  );
  return toAction(result, (suggestion) => ({ suggestion }));
}
