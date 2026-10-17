// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
"use server";

/**
 * Start a counterfactual fairness run (M3, brief 3.3.4) for the signed-in person. The API
 * checks `hire.fairness.run` and allows one run at a time per organisation.
 */
import type { ActionResult } from "../action-result";
import { callApi, toAction } from "../api/call";

export async function startFairnessRun(): Promise<ActionResult<{ id: string }>> {
  const result = await callApi((api) => api.POST("/v1/hire/fairness/runs", { body: { sample_size: 10, threshold: 5 } }));
  return toAction(result, (run) => ({ id: run.id }));
}
