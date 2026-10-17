// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
"use server";

/**
 * Server actions of the candidate portal (WP2.9). Each one checks its input shape and
 * calls the portal API with the candidate's portal session from its HTTP-only cookie
 * (`../portal/session`). The API decides everything; the browser never sees the token.
 */
import { z } from "zod";

import type { ActionResult } from "../action-result";
import { clearPortalToken, portalCall, setPortalToken } from "../portal/session";
import {
  isLoginToken,
  type PortalAppeal,
  type PortalApplication,
  type PortalApplicationDetail,
  type PortalMessage,
  type PortalPrivacy,
} from "../portal/steps";

const uuid = z.uuid();
const bad = { ok: false as const, code: "invalid_input" };
const ORG = /^[a-z0-9][a-z0-9-]{0,79}$/;

function fail(result: { code: string }): { ok: false; code: string } {
  return { ok: false, code: result.code };
}

/** Ask for a magic link. The answer is the same whether or not anyone applied. */
export async function requestPortalLink(input: {
  org: string;
  email: string;
  locale: "en" | "ar";
}): Promise<ActionResult> {
  const parsed = z
    .object({ org: z.string().regex(ORG), email: z.email().max(320), locale: z.enum(["en", "ar"]) })
    .safeParse(input);
  if (!parsed.success) return bad;
  const result = await portalCall("/v1/hire/portal/login-link", {
    method: "POST",
    body: parsed.data,
    session: false,
  });
  return result.ok ? { ok: true } : fail(result);
}

/** Use a magic link once: the portal session goes into its HTTP-only cookie. */
export async function openPortalSession(token: string): Promise<ActionResult<{ locale: string }>> {
  if (!isLoginToken(token)) return { ok: false, code: "not_found" };
  const result = await portalCall<{ session_token: string; expires_at: string; locale: string }>(
    "/v1/hire/portal/session",
    { method: "POST", body: { token }, session: false },
  );
  if (!result.ok) return fail(result);
  await setPortalToken(result.data.session_token, result.data.expires_at);
  return { ok: true, locale: result.data.locale };
}

export async function signOutOfPortal(): Promise<ActionResult> {
  await portalCall("/v1/hire/portal/sign-out", { method: "POST" });
  await clearPortalToken();
  return { ok: true };
}

export async function loadPortalApplications(): Promise<ActionResult<{ applications: PortalApplication[] }>> {
  const result = await portalCall<PortalApplication[]>("/v1/hire/portal/applications");
  return result.ok ? { ok: true, applications: result.data } : fail(result);
}

export async function loadPortalApplication(id: string): Promise<ActionResult<{ application: PortalApplicationDetail }>> {
  if (!uuid.safeParse(id).success) return bad;
  const result = await portalCall<PortalApplicationDetail>(`/v1/hire/portal/applications/${id}`);
  return result.ok ? { ok: true, application: result.data } : fail(result);
}

export async function withdrawApplication(id: string): Promise<ActionResult<{ application: PortalApplicationDetail }>> {
  if (!uuid.safeParse(id).success) return bad;
  const result = await portalCall<PortalApplicationDetail>(`/v1/hire/portal/applications/${id}/withdraw`, {
    method: "POST",
    body: { confirm: true },
  });
  return result.ok ? { ok: true, application: result.data } : fail(result);
}

export async function replyToCompany(id: string, text: string): Promise<ActionResult<{ message: PortalMessage }>> {
  const parsed = z.object({ id: uuid, text: z.string().trim().min(1).max(8000) }).safeParse({ id, text });
  if (!parsed.success) return bad;
  const result = await portalCall<PortalMessage>(`/v1/hire/portal/applications/${parsed.data.id}/messages`, {
    method: "POST",
    body: { text: parsed.data.text },
  });
  return result.ok ? { ok: true, message: result.data } : fail(result);
}

export async function loadPrivacy(): Promise<ActionResult<{ privacy: PortalPrivacy }>> {
  const result = await portalCall<PortalPrivacy>("/v1/hire/portal/privacy");
  return result.ok ? { ok: true, privacy: result.data } : fail(result);
}

export async function requestDeletion(note: string): Promise<ActionResult<{ created: boolean }>> {
  const parsed = z.string().trim().max(1000).safeParse(note);
  if (!parsed.success) return bad;
  const result = await portalCall<{ created: boolean }>("/v1/hire/portal/privacy/deletion-request", {
    method: "POST",
    body: parsed.data ? { note: parsed.data } : {},
  });
  return result.ok ? { ok: true, created: result.data.created } : fail(result);
}

export async function requestHumanReview(
  decisionId: string,
  reason: string,
): Promise<ActionResult<{ appeal: PortalAppeal }>> {
  const parsed = z
    .object({ decisionId: z.string().regex(/^dec_[0-9A-HJKMNP-TV-Z]{26}$/), reason: z.string().trim().min(10).max(4000) })
    .safeParse({ decisionId, reason });
  if (!parsed.success) return bad;
  const result = await portalCall<PortalAppeal>("/v1/hire/portal/privacy/appeals", {
    method: "POST",
    body: { decision_id: parsed.data.decisionId, reason: parsed.data.reason },
  });
  return result.ok ? { ok: true, appeal: result.data } : fail(result);
}
