// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
"use server";

/**
 * Server actions for decision records: replay, file and resolve appeals. The API scopes
 * every call to the workflows the actor may read, checks `core.decision.replay`,
 * `core.appeal.create`, the workflow decide permission and the TOTP step-up.
 */
import { z } from "zod";

import type { ActionResult } from "../action-result";
import { callApi, toAction } from "../api/call";
import type { Appeal, Replay } from "../api/types";
import { isDecisionId } from "../decision-id";

export async function replayDecision(
  decisionId: string,
  mode: "recorded" | "live" = "recorded",
): Promise<ActionResult<{ replay: Replay }>> {
  if (!isDecisionId(decisionId) || !["recorded", "live"].includes(mode)) return { ok: false, code: "invalid_input" };
  const result = await callApi((api) =>
    api.POST("/v1/decisions/{decision_id}/replay", {
      params: { path: { decision_id: decisionId } },
      body: { mode },
    }),
  );
  return toAction(result, (replay) => ({ replay }));
}

const appealSchema = z.object({
  requesterType: z.enum(["candidate", "vendor", "staff"]),
  requesterId: z.string().trim().min(1).max(128),
  reason: z.string().trim().min(1).max(4000),
});

export async function fileAppeal(
  decisionId: string,
  input: z.infer<typeof appealSchema>,
): Promise<ActionResult<{ appeal: Appeal }>> {
  const parsed = appealSchema.safeParse(input);
  if (!isDecisionId(decisionId) || !parsed.success) return { ok: false, code: "invalid_input" };
  const result = await callApi((api) =>
    api.POST("/v1/decisions/{decision_id}/appeals", {
      params: { path: { decision_id: decisionId } },
      body: {
        requester_type: parsed.data.requesterType,
        requester_id: parsed.data.requesterId,
        reason: parsed.data.reason,
      },
    }),
  );
  return toAction(result, (appeal) => ({ appeal }));
}

const resolveSchema = z.object({
  appealId: z.uuid(),
  outcome: z.enum(["upheld", "overturned", "partially_overturned"]),
  outcomeText: z.string().trim().min(1).max(4000),
});

export async function resolveAppeal(input: z.infer<typeof resolveSchema>): Promise<ActionResult<{ appeal: Appeal }>> {
  const parsed = resolveSchema.safeParse(input);
  if (!parsed.success) return { ok: false, code: "invalid_input" };
  const result = await callApi((api) =>
    api.POST("/v1/appeals/{appeal_id}/resolve", {
      params: { path: { appeal_id: parsed.data.appealId } },
      body: { outcome: parsed.data.outcome, outcome_text: parsed.data.outcomeText },
    }),
  );
  return toAction(result, (appeal) => ({ appeal }));
}
