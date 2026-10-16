// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
"use server";

/**
 * Server actions for the pre-employment checklist (M3, brief 3.8.4), for HR and admins.
 * Each one checks its input shape and calls the API with the user's token; the API
 * decides who may see and change the checklist.
 */
import { z } from "zod";

import type { ActionResult } from "../action-result";
import { callApi, toAction } from "../api/call";
import type { HireChecklist, HireChecklistConfig } from "../hire/types";

const uuid = z.uuid();
const bad = { ok: false as const, code: "invalid_input" };

/** Ask the candidate, through the portal, for the documents not requested yet. */
export async function requestChecklist(applicationId: string): Promise<ActionResult<{ checklist: HireChecklist }>> {
  if (!uuid.safeParse(applicationId).success) return bad;
  const result = await callApi((api) =>
    api.POST("/v1/hire/applications/{application_id}/checklist", { params: { path: { application_id: applicationId } } }),
  );
  return toAction(result, (checklist) => ({ checklist }));
}

export async function reviewChecklistItem(
  itemId: string,
  outcome: "verified" | "waived",
): Promise<ActionResult<{ checklist: HireChecklist }>> {
  if (!uuid.safeParse(itemId).success || !["verified", "waived"].includes(outcome)) return bad;
  const result = await callApi((api) =>
    api.POST("/v1/hire/checklist-items/{item_id}/review", { params: { path: { item_id: itemId } }, body: { outcome } }),
  );
  return toAction(result, (checklist) => ({ checklist }));
}

const configInput = z.object({
  jobId: uuid,
  items: z
    .array(
      z.object({
        key: z.string().regex(/^[a-z][a-z0-9_]{0,31}$/),
        label: z.string().trim().min(2).max(120),
        label_ar: z.string().trim().max(120),
      }),
    )
    .min(1)
    .max(12),
  retentionDays: z.number().int().min(1).max(365),
});

/** The documents to collect for a job and how long files are kept after hire. */
export async function saveChecklistConfig(input: z.infer<typeof configInput>): Promise<ActionResult<{ config: HireChecklistConfig }>> {
  const parsed = configInput.safeParse(input);
  if (!parsed.success) return bad;
  const { jobId, items, retentionDays } = parsed.data;
  const result = await callApi((api) =>
    api.PUT("/v1/hire/jobs/{job_id}/checklist", {
      params: { path: { job_id: jobId } },
      body: { items, retention_days: retentionDays },
    }),
  );
  return toAction(result, (config) => ({ config }));
}
