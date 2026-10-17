// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
"use server";

/**
 * Server actions for creating a job and its rubric (brief 5.3): the AI drafts of the
 * description and the rubric, the text checks, saving, locking and publishing. Each one
 * checks its input shape and calls the API with the signed-in user's token; the API
 * decides permissions and the second factor (locking and publishing need one).
 */
import { z } from "zod";

import type { ActionResult } from "../action-result";
import { callApi, toAction } from "../api/call";
import type { HireJob, HireJobDraft, HireJobInput, HireRubricLock, HireRubricState, HireTextChecks } from "../hire/types";

const uuid = z.uuid();
const bad = { ok: false as const, code: "invalid_input" };

const money = z.string().regex(/^\d{1,12}(\.\d{1,2})?$/).nullable();
const jobInput = z.object({
  title: z.string().trim().min(2).max(160),
  title_ar: z.string().max(160),
  team: z.string().max(160).nullable(),
  location: z.string().trim().min(2).max(160),
  country: z.string().regex(/^[A-Z]{2}$/).nullable(),
  work_model: z.enum(["onsite", "hybrid", "remote"]),
  employment_type: z.enum(["full_time", "part_time", "contract", "temporary", "internship"]),
  salary_min: money,
  salary_max: money,
  salary_currency: z.string().regex(/^[A-Z]{3}$/).nullable(),
  salary_period: z.enum(["year", "month", "hour"]).nullable(),
  show_salary: z.boolean(),
  description_en: z.string().max(20_000),
  description_ar: z.string().max(20_000),
});

export async function draftJobDescription(brief: string): Promise<ActionResult<{ draft: HireJobDraft }>> {
  const parsed = z.string().trim().min(10).max(800).safeParse(brief);
  if (!parsed.success) return bad;
  const result = await callApi((api) => api.POST("/v1/hire/jobs/draft-description", { body: { brief: parsed.data } }));
  return toAction(result, (draft) => ({ draft }));
}

export async function checkJobText(input: {
  title: string;
  title_ar: string;
  description_en: string;
  description_ar: string;
}): Promise<ActionResult<{ checks: HireTextChecks }>> {
  const parsed = z
    .object({
      title: z.string().max(160),
      title_ar: z.string().max(160),
      description_en: z.string().max(20_000),
      description_ar: z.string().max(20_000),
    })
    .safeParse(input);
  if (!parsed.success) return bad;
  const result = await callApi((api) => api.POST("/v1/hire/jobs/check-text", { body: parsed.data }));
  return toAction(result, (checks) => ({ checks }));
}

export async function createJob(input: HireJobInput): Promise<ActionResult<{ job: HireJob }>> {
  const parsed = jobInput.safeParse(input);
  if (!parsed.success) return bad;
  const result = await callApi((api) => api.POST("/v1/hire/jobs", { body: parsed.data }));
  return toAction(result, (job) => ({ job }));
}

export async function updateJob(jobId: string, input: HireJobInput): Promise<ActionResult<{ job: HireJob }>> {
  const parsed = jobInput.safeParse(input);
  if (!uuid.safeParse(jobId).success || !parsed.success) return bad;
  const result = await callApi((api) =>
    api.PATCH("/v1/hire/jobs/{job_id}", { params: { path: { job_id: jobId } }, body: parsed.data }),
  );
  return toAction(result, (job) => ({ job }));
}

export async function draftJobRubric(jobId: string): Promise<ActionResult<{ rubric: HireRubricState }>> {
  if (!uuid.safeParse(jobId).success) return bad;
  const result = await callApi((api) =>
    api.POST("/v1/hire/jobs/{job_id}/rubric/draft", { params: { path: { job_id: jobId } } }),
  );
  return toAction(result, (rubric) => ({ rubric }));
}

export async function saveJobRubric(
  jobId: string,
  rubric: Record<string, unknown>,
): Promise<ActionResult<{ rubric: HireRubricState }>> {
  const size = JSON.stringify(rubric ?? null).length;
  if (!uuid.safeParse(jobId).success || typeof rubric !== "object" || rubric === null || size > 100_000) return bad;
  const result = await callApi((api) =>
    api.PUT("/v1/hire/jobs/{job_id}/rubric", { params: { path: { job_id: jobId } }, body: rubric }),
  );
  return toAction(result, (state) => ({ rubric: state }));
}

export async function lockJobRubric(jobId: string): Promise<ActionResult<{ lock: HireRubricLock }>> {
  if (!uuid.safeParse(jobId).success) return bad;
  const result = await callApi((api) =>
    api.POST("/v1/hire/jobs/{job_id}/rubric/lock", { params: { path: { job_id: jobId } } }),
  );
  return toAction(result, (lock) => ({ lock }));
}

export async function publishJob(jobId: string): Promise<ActionResult<{ job: HireJob }>> {
  if (!uuid.safeParse(jobId).success) return bad;
  const result = await callApi((api) =>
    api.POST("/v1/hire/jobs/{job_id}/publish", { params: { path: { job_id: jobId } } }),
  );
  return toAction(result, (job) => ({ job }));
}
