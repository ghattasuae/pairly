// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
"use server";

/**
 * The candidate's answer to an offer in the portal (M3): accept or decline, once,
 * confirmed. The API records it as the candidate's decision and audits it.
 */
import { z } from "zod";

import type { ActionResult } from "../action-result";
import type { HirePortalOffer } from "../hire/types";
import { portalCall } from "../portal/session";

const bad = { ok: false as const, code: "invalid_input" };

export async function answerOffer(input: {
  offerId: string;
  accept: boolean;
  note: string;
}): Promise<ActionResult<{ offer: HirePortalOffer }>> {
  const parsed = z.object({ offerId: z.uuid(), accept: z.boolean(), note: z.string().trim().max(1000) }).safeParse(input);
  if (!parsed.success) return bad;
  const { offerId, accept, note } = parsed.data;
  const result = await portalCall<HirePortalOffer>(`/v1/hire/portal/offers/${offerId}/${accept ? "accept" : "decline"}`, {
    method: "POST",
    body: { confirm: true, note: note || null },
  });
  return result.ok ? { ok: true, offer: result.data } : { ok: false, code: result.code };
}
