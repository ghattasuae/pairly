// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
"use server";

/**
 * Server actions for the approval inbox and signed approval links. Each one re-checks its
 * input and calls the API with the signed-in user's token. The API decides everything
 * (workflow permission, segregation of duties, assignment, TOTP step-up); nothing here does.
 */
import { z } from "zod";

import type { ActionResult } from "../action-result";
import { callApi, toAction } from "../api/call";
import type { ApprovalDetail, ApprovalLinkView, ApprovalSummary, DecisionResult } from "../api/types";
import { getSession } from "../auth/server";
import { apiQuery, type InboxFilters, isUuid, STATUSES, SLA_STATES } from "../inbox-filters";

const filtersSchema = z.object({
  workflow: z
    .string()
    .regex(/^[a-z][a-z0-9_]{0,31}$/)
    .nullable(),
  status: z.enum(STATUSES),
  sla: z.enum(SLA_STATES).nullable(),
  mine: z.boolean(),
  q: z.string().max(100),
});

const decisionSchema = z
  .object({
    approvalId: z.uuid(),
    decision: z.enum(["approve", "reject", "request_changes"]),
    reason: z.string().trim().max(2000).nullable(),
    idempotencyKey: z.string().regex(/^[A-Za-z0-9._:-]{8,128}$/),
    stepId: z.uuid().nullable(),
  })
  .refine((d) => d.decision === "approve" || (d.reason !== null && d.reason.length > 0), {
    message: "reason required",
  });

export type DecisionInput = z.infer<typeof decisionSchema>;

/** One more page of the inbox (the first page is rendered on the server). */
export async function loadInboxPage(
  filters: InboxFilters,
  cursor: string,
): Promise<ActionResult<{ items: ApprovalSummary[]; nextCursor: string | null }>> {
  const parsed = filtersSchema.safeParse(filters);
  if (!parsed.success || cursor.length > 256) return { ok: false, code: "invalid_input" };
  const result = await callApi((api) => api.GET("/v1/approvals", { params: { query: apiQuery(parsed.data, cursor) } }));
  return toAction(result, (data) => ({ items: data.items, nextCursor: data.next_cursor }));
}

/** One approval with its context, chain and history. */
export async function loadApproval(approvalId: string): Promise<ActionResult<{ detail: ApprovalDetail }>> {
  if (!isUuid(approvalId)) return { ok: false, code: "invalid_input" };
  const result = await callApi((api) =>
    api.GET("/v1/approvals/{approval_id}", { params: { path: { approval_id: approvalId } } }),
  );
  return toAction(result, (detail) => ({ detail }));
}

/** Record a decision. The same `idempotencyKey` and body repeat safely. */
export async function decideApproval(input: DecisionInput): Promise<ActionResult<{ result: DecisionResult }>> {
  const parsed = decisionSchema.safeParse(input);
  if (!parsed.success) return { ok: false, code: "invalid_input" };
  const { approvalId, decision, reason, idempotencyKey, stepId } = parsed.data;
  const result = await callApi((api) =>
    api.POST("/v1/approvals/{approval_id}/decision", {
      params: { path: { approval_id: approvalId }, header: { "Idempotency-Key": idempotencyKey } },
      body: { decision, reason: reason || null, step_id: stepId },
    }),
  );
  return toAction(result, (data) => ({ result: data }));
}

// ------------------------------------------------------------------ approval links

const tokenSchema = z.string().regex(/^[A-Za-z0-9_-]{43}$/);

export type LinkDescription =
  | { kind: "signedOut" }
  /** Unknown, used, expired, invalidated, or sent to someone else: one uniform answer. */
  | { kind: "invalid"; email: string | null }
  | { kind: "unavailable" }
  | { kind: "ready"; link: ApprovalLinkView; email: string | null };

/** What an approval link would do, for the signed-in user. Changes nothing. */
export async function describeApprovalLink(token: string): Promise<LinkDescription> {
  if (!tokenSchema.safeParse(token).success) return { kind: "invalid", email: null };
  const session = await getSession();
  if (!session) return { kind: "signedOut" };
  const email = session.user.email ?? null;
  const result = await callApi((api) => api.POST("/v1/approval-links/describe", { body: { token } }), {
    scope: "user",
  });
  if (result.ok) {
    if (result.data.state !== "valid") return { kind: "invalid", email };
    return { kind: "ready", link: result.data, email };
  }
  if (result.code === "not_signed_in") return { kind: "signedOut" };
  if (result.code === "unavailable" || result.code === "rate_limited") return { kind: "unavailable" };
  return { kind: "invalid", email };
}

const confirmSchema = z.object({ token: tokenSchema, reason: z.string().trim().max(2000).nullable() });

/** Confirm the link's decision as the signed-in (bound) user. */
export async function confirmApprovalLink(
  token: string,
  reason: string | null,
): Promise<ActionResult<{ result: DecisionResult }>> {
  const parsed = confirmSchema.safeParse({ token, reason });
  if (!parsed.success) return { ok: false, code: "invalid_input" };
  const result = await callApi(
    (api) =>
      api.POST("/v1/approval-links/confirm", {
        body: { token: parsed.data.token, reason: parsed.data.reason || null },
      }),
    { scope: "user" },
  );
  if (!result.ok && (result.status === 410 || result.status === 404)) return { ok: false, code: "gone" };
  return toAction(result, (data) => ({ result: data }));
}
