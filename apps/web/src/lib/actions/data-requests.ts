// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
"use server";

/**
 * Server actions for the admins' candidate data requests page. The API decides
 * everything (permission, second factor, people only); nothing here does. Erasing a
 * candidate's data is always a person's explicit action with a written reason.
 */
import { z } from "zod";

import type { ActionResult } from "../action-result";
import { callApi, toAction } from "../api/call";
import type { HireDataRequest } from "../hire/types";

const uuid = z.uuid();
const bad = { ok: false as const, code: "invalid_input" };

export async function loadDataRequest(id: string): Promise<ActionResult<{ request: HireDataRequest }>> {
  if (!uuid.safeParse(id).success) return bad;
  const result = await callApi((api) =>
    api.GET("/v1/hire/data-requests/{request_id}", { params: { path: { request_id: id } } }),
  );
  return toAction(result, (request) => ({ request }));
}

export async function resolveDataRequest(input: {
  id: string;
  status: "completed" | "declined";
  outcome: string;
}): Promise<ActionResult<{ request: HireDataRequest }>> {
  const parsed = z
    .object({ id: uuid, status: z.enum(["completed", "declined"]), outcome: z.string().trim().min(5).max(2000) })
    .safeParse(input);
  if (!parsed.success) return bad;
  const result = await callApi((api) =>
    api.POST("/v1/hire/data-requests/{request_id}/resolve", {
      params: { path: { request_id: parsed.data.id } },
      body: { status: parsed.data.status, outcome: parsed.data.outcome },
    }),
  );
  return toAction(result, (request) => ({ request }));
}

export async function eraseDataRequest(input: {
  id: string;
  reason: string;
}): Promise<ActionResult<{ request: HireDataRequest }>> {
  const parsed = z.object({ id: uuid, reason: z.string().trim().min(10).max(2000) }).safeParse(input);
  if (!parsed.success) return bad;
  const result = await callApi((api) =>
    api.POST("/v1/hire/data-requests/{request_id}/erase", {
      params: { path: { request_id: parsed.data.id } },
      body: { reason: parsed.data.reason },
    }),
  );
  return toAction(result, (request) => ({ request }));
}
