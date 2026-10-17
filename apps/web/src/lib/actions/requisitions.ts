// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
"use server";

/**
 * Server actions for requisitions (M3, brief 3.1.1). Each one checks its input shape and
 * calls the API with the signed-in user's token. The API decides everything: who may
 * request, the approval chain, segregation of duties (the requester never approves) and
 * that a job opens only from an approved requisition.
 */
import { z } from "zod";

import type { ActionResult } from "../action-result";
import { callApi, toAction } from "../api/call";
import type { HireJob, HireRequisition } from "../hire/types";

const uuid = z.uuid();
const key = z.string().regex(/^[A-Za-z0-9._:-]{8,128}$/);
const bad = { ok: false as const, code: "invalid_input" };

const requisitionInput = z.object({
  title: z.string().trim().min(1).max(160),
  title_ar: z.string().trim().max(160),
  team: z.string().trim().min(1).max(120),
  budget: z.string().regex(/^\d{1,12}(\.\d{1,2})?$/),
  currency: z.string().regex(/^[A-Z]{3}$/),
  headcount: z.number().int().min(1).max(100),
  location: z.string().trim().min(1).max(160),
  work_model: z.enum(["onsite", "hybrid", "remote"]),
  start_date: z.string().regex(/^\d{4}-\d{2}-\d{2}$/),
  justification: z.string().trim().max(1000),
});

export type RequisitionFormInput = z.infer<typeof requisitionInput>;

export async function createRequisition(
  input: RequisitionFormInput,
  idempotencyKey: string,
): Promise<ActionResult<{ requisition: HireRequisition }>> {
  const parsed = requisitionInput.safeParse(input);
  if (!parsed.success || !key.safeParse(idempotencyKey).success) return bad;
  const result = await callApi((api) =>
    api.POST("/v1/hire/requisitions", {
      body: parsed.data,
      headers: { "Idempotency-Key": idempotencyKey },
    }),
  );
  return toAction(result, (requisition) => ({ requisition }));
}

export async function cancelRequisition(id: string): Promise<ActionResult<{ requisition: HireRequisition }>> {
  if (!uuid.safeParse(id).success) return bad;
  const result = await callApi((api) =>
    api.POST("/v1/hire/requisitions/{requisition_id}/cancel", { params: { path: { requisition_id: id } } }),
  );
  return toAction(result, (requisition) => ({ requisition }));
}

export async function openJobFromRequisition(
  id: string,
  idempotencyKey: string,
): Promise<ActionResult<{ job: HireJob }>> {
  if (!uuid.safeParse(id).success || !key.safeParse(idempotencyKey).success) return bad;
  const result = await callApi((api) =>
    api.POST("/v1/hire/requisitions/{requisition_id}/job", {
      params: { path: { requisition_id: id } },
      headers: { "Idempotency-Key": idempotencyKey },
    }),
  );
  return toAction(result, (job) => ({ job }));
}
