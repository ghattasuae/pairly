// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
"use server";

/**
 * Server actions for offers (M3, brief 3.8.3), for signed-in people. Each one checks its
 * input shape and calls the API with the user's token; the API decides who sees and
 * sets compensation, the approval chain and when an offer may be sent.
 */
import { z } from "zod";

import type { ActionResult } from "../action-result";
import { callApi, toAction } from "../api/call";
import type { HireOffer } from "../hire/types";

const uuid = z.uuid();
const bad = { ok: false as const, code: "invalid_input" };
const amount = z.string().regex(/^\d{1,8}(\.\d{1,2})?$/);

const offerInput = z.object({
  applicationId: uuid,
  template: z.enum(["standard", "fixed_term"]),
  startDate: z.iso.date(),
  locale: z.enum(["en", "ar"]),
  jobTitle: z.string().trim().max(160),
  conditions: z.array(z.string().trim().min(1).max(300)).max(12),
  compensation: z.object({
    currency: z.string().regex(/^[A-Z]{3}$/),
    base_monthly: amount,
    housing_monthly: amount,
    transport_monthly: amount,
    other_monthly: amount,
    bonus_target_pct: amount,
  }),
});

export type OfferInput = z.infer<typeof offerInput>;

/** Build an offer from a template (a draft). */
export async function createOffer(input: OfferInput): Promise<ActionResult<{ offer: HireOffer }>> {
  const parsed = offerInput.safeParse(input);
  if (!parsed.success) return bad;
  const d = parsed.data;
  const result = await callApi((api) =>
    api.POST("/v1/hire/applications/{application_id}/offers", {
      params: { path: { application_id: d.applicationId } },
      body: {
        template: d.template,
        start_date: d.startDate,
        locale: d.locale,
        job_title: d.jobTitle || null,
        conditions: d.conditions,
        compensation: d.compensation,
      },
    }),
  );
  return toAction(result, (offer) => ({ offer }));
}

type OfferAction = "submit" | "send" | "withdraw";

/** Submit for approval, send after approval, or withdraw. */
export async function offerAction(offerId: string, action: OfferAction): Promise<ActionResult<{ offer: HireOffer }>> {
  if (!uuid.safeParse(offerId).success || !["submit", "send", "withdraw"].includes(action)) return bad;
  const params = { params: { path: { offer_id: offerId } } };
  const result = await callApi((api) =>
    action === "submit"
      ? api.POST("/v1/hire/offers/{offer_id}/submit", params)
      : action === "send"
        ? api.POST("/v1/hire/offers/{offer_id}/send", params)
        : api.POST("/v1/hire/offers/{offer_id}/withdraw", params),
  );
  return toAction(result, (offer) => ({ offer }));
}
