// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
"use server";

/**
 * Server actions for reference checks (M3, brief 3.8.2), for signed-in people. Each one
 * checks its input shape and calls the API with the user's token; the API decides who
 * may ask referees and what stays hidden while the candidate is blind.
 */
import { z } from "zod";

import type { ActionResult } from "../action-result";
import { callApi, toAction } from "../api/call";
import type { HireReferences } from "../hire/types";

const uuid = z.uuid();
const bad = { ok: false as const, code: "invalid_input" };

const refereeInput = z.object({
  applicationId: uuid,
  name: z.string().trim().min(2).max(120),
  email: z.email().max(254),
  relationship: z.enum(["manager", "peer", "direct_report", "client", "other"]),
  locale: z.enum(["en", "ar"]),
});

export type RefereeInput = z.infer<typeof refereeInput>;

/** Ask a referee: the email with their single use link goes out at once. */
export async function askReferee(input: RefereeInput): Promise<ActionResult<{ references: HireReferences }>> {
  const parsed = refereeInput.safeParse(input);
  if (!parsed.success) return bad;
  const { applicationId, ...body } = parsed.data;
  const result = await callApi((api) =>
    api.POST("/v1/hire/applications/{application_id}/references", {
      params: { path: { application_id: applicationId } },
      body,
    }),
  );
  return toAction(result, (references) => ({ references }));
}

/** Send the referee a new link (the old one stops working). */
export async function resendReference(referenceId: string): Promise<ActionResult<{ references: HireReferences }>> {
  if (!uuid.safeParse(referenceId).success) return bad;
  const result = await callApi((api) =>
    api.POST("/v1/hire/references/{reference_id}/send", { params: { path: { reference_id: referenceId } } }),
  );
  return toAction(result, (references) => ({ references }));
}

export async function cancelReference(referenceId: string): Promise<ActionResult<{ references: HireReferences }>> {
  if (!uuid.safeParse(referenceId).success) return bad;
  const result = await callApi((api) =>
    api.POST("/v1/hire/references/{reference_id}/cancel", { params: { path: { reference_id: referenceId } } }),
  );
  return toAction(result, (references) => ({ references }));
}
