// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
"use server";

/** Server actions for the audit viewer: check the hash chain (read only). */
import { z } from "zod";

import type { ActionResult } from "../action-result";
import { callApi } from "../api/call";
import type { ChainCheck } from "../api/types";

export type ChainStatus =
  | { state: "done"; check: ChainCheck }
  | { state: "queued"; jobId: string }
  | { state: "failed"; error: string | null };

/** Recompute the chain now, or queue it for the worker when it is long (202). */
export async function verifyChain(): Promise<ActionResult<{ status: ChainStatus }>> {
  const result = await callApi((api) => api.GET("/v1/audit/verify"));
  if (!result.ok) return { ok: false, code: result.code };
  const body = result.data as unknown as Record<string, unknown>;
  if (result.status === 202 && typeof body.job_id === "string") {
    return { ok: true, status: { state: "queued", jobId: body.job_id } };
  }
  return { ok: true, status: { state: "done", check: result.data as ChainCheck } };
}

/** The state of a queued chain check. */
export async function chainJob(jobId: string): Promise<ActionResult<{ status: ChainStatus }>> {
  if (!z.uuid().safeParse(jobId).success) return { ok: false, code: "invalid_input" };
  const result = await callApi((api) => api.GET("/v1/audit/verify/{job_id}", { params: { path: { job_id: jobId } } }));
  if (!result.ok) return { ok: false, code: result.code };
  const job = result.data;
  if (job.status === "done" || job.status === "completed" || job.status === "succeeded") {
    return job.result
      ? { ok: true, status: { state: "done", check: job.result as unknown as ChainCheck } }
      : { ok: true, status: { state: "failed", error: job.error ?? null } };
  }
  if (job.status === "failed" || job.status === "error") {
    return { ok: true, status: { state: "failed", error: job.error ?? null } };
  }
  return { ok: true, status: { state: "queued", jobId } };
}
