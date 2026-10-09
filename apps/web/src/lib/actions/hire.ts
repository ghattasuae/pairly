// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
"use server";

/**
 * Server actions for the Hire pipeline and messaging (WP2.5, WP2.7). Each one checks its
 * input shape and calls the API with the signed-in user's token. The API decides
 * everything (permissions, second factor, blind mode, segregation of duties on
 * approvals); nothing here does.
 *
 * Writes that a retry could repeat (moves, sends, submissions) carry an Idempotency-Key
 * chosen by the browser, so a retry after a network failure never acts twice.
 */
import { z } from "zod";

import type { ActionResult } from "../action-result";
import { callApi, toAction } from "../api/call";
import type {
  HireAiDraft,
  HireBoard,
  HireBulkMessageSummary,
  HireBulkResult,
  HireBulkSummary,
  HireMessage,
  HireMove,
  HireProfile,
  HireRediscoveryHit,
  HireReferral,
  HireTeamMember,
  HireTemplate,
  HireUndo,
} from "../hire/types";

const uuid = z.uuid();
const key = z.string().regex(/^[A-Za-z0-9._:-]{8,128}$/);
const stageKey = z.string().regex(/^[a-z][a-z0-9_]{0,31}$/);
const templateKey = z.string().regex(/^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*){1,4}$/).max(96);
const ids = z.array(uuid).min(1).max(200);
const values = z.record(z.string().regex(/^[a-z][a-z0-9_]{0,47}$/), z.string().max(2000).nullable()).refine(
  (v) => Object.keys(v).length <= 20,
);

const bad = { ok: false as const, code: "invalid_input" };

function idempotency(k: string): { "Idempotency-Key": string } {
  return { "Idempotency-Key": k };
}

// ------------------------------------------------------------------ board and profile

export async function loadBoard(jobId: string): Promise<ActionResult<{ board: HireBoard }>> {
  if (!uuid.safeParse(jobId).success) return bad;
  const result = await callApi((api) =>
    api.GET("/v1/hire/jobs/{job_id}/pipeline", { params: { path: { job_id: jobId } } }),
  );
  return toAction(result, (board) => ({ board }));
}

export async function loadProfile(applicationId: string): Promise<ActionResult<{ profile: HireProfile }>> {
  if (!uuid.safeParse(applicationId).success) return bad;
  const result = await callApi((api) =>
    api.GET("/v1/hire/pipeline/applications/{application_id}", {
      params: { path: { application_id: applicationId } },
    }),
  );
  return toAction(result, (profile) => ({ profile }));
}

// ------------------------------------------------------------------ moves

export async function moveCandidate(input: {
  applicationId: string;
  toStage: string;
  idempotencyKey: string;
}): Promise<ActionResult<{ move: HireMove }>> {
  const parsed = z
    .object({ applicationId: uuid, toStage: stageKey, idempotencyKey: key })
    .safeParse(input);
  if (!parsed.success) return bad;
  const { applicationId, toStage, idempotencyKey } = parsed.data;
  const result = await callApi((api) =>
    api.POST("/v1/hire/pipeline/applications/{application_id}/move", {
      params: { path: { application_id: applicationId } },
      body: { to_stage: toStage },
      headers: idempotency(idempotencyKey),
    }),
  );
  return toAction(result, (move) => ({ move }));
}

export async function advanceCandidate(input: {
  applicationId: string;
  idempotencyKey: string;
}): Promise<ActionResult<{ move: HireMove }>> {
  const parsed = z.object({ applicationId: uuid, idempotencyKey: key }).safeParse(input);
  if (!parsed.success) return bad;
  const result = await callApi((api) =>
    api.POST("/v1/hire/pipeline/applications/{application_id}/advance", {
      params: { path: { application_id: parsed.data.applicationId } },
      headers: idempotency(parsed.data.idempotencyKey),
    }),
  );
  return toAction(result, (move) => ({ move }));
}

export async function undoMoves(moveIds: string[]): Promise<ActionResult<{ undo: HireUndo }>> {
  if (!ids.safeParse(moveIds).success) return bad;
  const result = await callApi((api) =>
    api.POST("/v1/hire/pipeline/moves/undo", { body: { move_ids: moveIds } }),
  );
  return toAction(result, (undo) => ({ undo }));
}

export async function bulkPreview(input: {
  jobId: string;
  applicationIds: string[];
  toStage: string;
}): Promise<ActionResult<{ summary: HireBulkSummary }>> {
  const parsed = z.object({ jobId: uuid, applicationIds: ids, toStage: stageKey }).safeParse(input);
  if (!parsed.success) return bad;
  const result = await callApi((api) =>
    api.POST("/v1/hire/jobs/{job_id}/pipeline/bulk/preview", {
      params: { path: { job_id: parsed.data.jobId } },
      body: { application_ids: parsed.data.applicationIds, to_stage: parsed.data.toStage },
    }),
  );
  return toAction(result, (summary) => ({ summary }));
}

export async function bulkMove(input: {
  jobId: string;
  applicationIds: string[];
  toStage: string;
  idempotencyKey: string;
}): Promise<ActionResult<{ result: HireBulkResult }>> {
  const parsed = z
    .object({ jobId: uuid, applicationIds: ids, toStage: stageKey, idempotencyKey: key })
    .safeParse(input);
  if (!parsed.success) return bad;
  const result = await callApi((api) =>
    api.POST("/v1/hire/jobs/{job_id}/pipeline/bulk/move", {
      params: { path: { job_id: parsed.data.jobId } },
      body: { application_ids: parsed.data.applicationIds, to_stage: parsed.data.toStage },
      headers: idempotency(parsed.data.idempotencyKey),
    }),
  );
  return toAction(result, (data) => ({ result: data }));
}

// ------------------------------------------------------------------ notes and tags

export async function addNote(input: {
  applicationId: string;
  body: string;
  mentions: string[];
}): Promise<ActionResult<{ noteId: string }>> {
  const parsed = z
    .object({
      applicationId: uuid,
      body: z.string().trim().min(1).max(4000),
      mentions: z.array(uuid).max(10),
    })
    .safeParse(input);
  if (!parsed.success) return bad;
  const result = await callApi(
    (api) =>
      api.POST("/v1/hire/pipeline/applications/{application_id}/notes", {
        params: { path: { application_id: parsed.data.applicationId } },
        body: { body: parsed.data.body, mentions: parsed.data.mentions },
      }),
    { okStatuses: [201] },
  );
  return toAction(result, (data) => ({ noteId: data.note_id }));
}

export async function setTags(applicationId: string, tags: string[]): Promise<ActionResult<{ tags: string[] }>> {
  const parsed = z
    .object({ applicationId: uuid, tags: z.array(z.string().trim().min(1).max(32)).max(12) })
    .safeParse({ applicationId, tags });
  if (!parsed.success) return bad;
  const result = await callApi((api) =>
    api.PUT("/v1/hire/pipeline/applications/{application_id}/tags", {
      params: { path: { application_id: parsed.data.applicationId } },
      body: { tags: parsed.data.tags },
    }),
  );
  return toAction(result, (data) => ({ tags: data.tags }));
}

export async function loadTeam(): Promise<ActionResult<{ team: HireTeamMember[] }>> {
  const result = await callApi((api) => api.GET("/v1/hire/pipeline/team"));
  return toAction(result, (team) => ({ team }));
}

// ------------------------------------------------------------------ messages

export async function loadTemplates(locale: "en" | "ar"): Promise<ActionResult<{ templates: HireTemplate[] }>> {
  const result = await callApi((api) =>
    api.GET("/v1/hire/messages/templates", { params: { query: { locale } } }),
  );
  return toAction(result, (templates) => ({ templates }));
}

export async function createMessage(input: {
  applicationId: string;
  templateKey: string;
  values?: Record<string, string | null>;
}): Promise<ActionResult<{ message: HireMessage }>> {
  const parsed = z
    .object({ applicationId: uuid, templateKey, values: values.optional() })
    .safeParse(input);
  if (!parsed.success) return bad;
  const result = await callApi(
    (api) =>
      api.POST("/v1/hire/pipeline/applications/{application_id}/messages", {
        params: { path: { application_id: parsed.data.applicationId } },
        body: { template_key: parsed.data.templateKey, values: parsed.data.values ?? {} },
      }),
    { okStatuses: [201] },
  );
  return toAction(result, (message) => ({ message }));
}

export async function loadMessage(messageId: string): Promise<ActionResult<{ message: HireMessage }>> {
  if (!uuid.safeParse(messageId).success) return bad;
  const result = await callApi((api) =>
    api.GET("/v1/hire/messages/{message_id}", { params: { path: { message_id: messageId } } }),
  );
  return toAction(result, (message) => ({ message }));
}

export async function editMessage(
  messageId: string,
  newValues: Record<string, string | null>,
): Promise<ActionResult<{ message: HireMessage }>> {
  if (!uuid.safeParse(messageId).success || !values.safeParse(newValues).success) return bad;
  const result = await callApi((api) =>
    api.PATCH("/v1/hire/messages/{message_id}", {
      params: { path: { message_id: messageId } },
      body: { values: newValues },
    }),
  );
  return toAction(result, (message) => ({ message }));
}

export async function aiDraftMessage(
  messageId: string,
  includeReason: boolean,
): Promise<ActionResult<{ draft: HireAiDraft }>> {
  if (!uuid.safeParse(messageId).success) return bad;
  const result = await callApi((api) =>
    api.POST("/v1/hire/messages/{message_id}/ai-draft", {
      params: { path: { message_id: messageId } },
      body: { include_reason: includeReason },
    }),
  );
  return toAction(result, (draft) => ({ draft }));
}

export async function sendMessage(
  messageId: string,
  idempotencyKey: string,
): Promise<ActionResult<{ message: HireMessage }>> {
  if (!uuid.safeParse(messageId).success || !key.safeParse(idempotencyKey).success) return bad;
  const result = await callApi((api) =>
    api.POST("/v1/hire/messages/{message_id}/send", {
      params: { path: { message_id: messageId } },
      headers: idempotency(idempotencyKey),
    }),
  );
  return toAction(result, (message) => ({ message }));
}

export async function submitMessage(
  messageId: string,
  idempotencyKey: string,
): Promise<ActionResult<{ message: HireMessage }>> {
  if (!uuid.safeParse(messageId).success || !key.safeParse(idempotencyKey).success) return bad;
  const result = await callApi((api) =>
    api.POST("/v1/hire/messages/{message_id}/submit", {
      params: { path: { message_id: messageId } },
      headers: idempotency(idempotencyKey),
    }),
  );
  return toAction(result, (message) => ({ message }));
}

export async function cancelMessage(messageId: string): Promise<ActionResult<{ message: HireMessage }>> {
  if (!uuid.safeParse(messageId).success) return bad;
  const result = await callApi((api) =>
    api.POST("/v1/hire/messages/{message_id}/cancel", { params: { path: { message_id: messageId } } }),
  );
  return toAction(result, (message) => ({ message }));
}

export async function bulkMessagePreview(input: {
  jobId: string;
  applicationIds: string[];
  templateKey: string;
}): Promise<ActionResult<{ summary: HireBulkMessageSummary }>> {
  const parsed = z.object({ jobId: uuid, applicationIds: ids, templateKey }).safeParse(input);
  if (!parsed.success) return bad;
  const result = await callApi((api) =>
    api.POST("/v1/hire/jobs/{job_id}/messages/bulk/preview", {
      params: { path: { job_id: parsed.data.jobId } },
      body: { application_ids: parsed.data.applicationIds, template_key: parsed.data.templateKey },
    }),
  );
  return toAction(result, (summary) => ({ summary }));
}

export async function bulkMessageSend(input: {
  jobId: string;
  applicationIds: string[];
  templateKey: string;
  idempotencyKey: string;
}): Promise<ActionResult<{ sent: number; suppressed: number }>> {
  const parsed = z
    .object({ jobId: uuid, applicationIds: ids, templateKey, idempotencyKey: key })
    .safeParse(input);
  if (!parsed.success) return bad;
  const result = await callApi((api) =>
    api.POST("/v1/hire/jobs/{job_id}/messages/bulk/send", {
      params: { path: { job_id: parsed.data.jobId } },
      body: { application_ids: parsed.data.applicationIds, template_key: parsed.data.templateKey },
      headers: idempotency(parsed.data.idempotencyKey),
    }),
  );
  return toAction(result, (data) => ({ sent: data.sent, suppressed: data.suppressed }));
}

// ------------------------------------------------------------------ rediscovery and referrals

export async function rediscover(query: string): Promise<ActionResult<{ hits: HireRediscoveryHit[] }>> {
  const parsed = z.string().trim().min(1).max(300).safeParse(query);
  if (!parsed.success) return bad;
  const result = await callApi((api) =>
    api.POST("/v1/hire/rediscovery/search", { body: { query: parsed.data, limit: 10 } }),
  );
  return toAction(result, (hits) => ({ hits }));
}

export async function referralLink(
  jobId: string,
  locale: "en" | "ar",
): Promise<ActionResult<{ url: string }>> {
  if (!uuid.safeParse(jobId).success) return bad;
  const result = await callApi((api) =>
    api.POST("/v1/hire/referrals/links", { body: { job_id: jobId, locale } }),
  );
  return toAction(result, (data) => ({ url: data.url }));
}

export async function myReferrals(): Promise<ActionResult<{ referrals: HireReferral[] }>> {
  const result = await callApi((api) => api.GET("/v1/hire/referrals/mine"));
  return toAction(result, (referrals) => ({ referrals }));
}
