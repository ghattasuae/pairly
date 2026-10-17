// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
"use server";

/**
 * Server actions of the referee's form (M3, brief 5.1.6). The browser never sees the
 * referee session: it lives in an HTTP-only cookie (`../reference/session`). The API
 * checks every answer and decides everything.
 */
import { z } from "zod";

import type { ActionResult } from "../action-result";
import type { HireRefereePage } from "../hire/types";
import { publicHireCall, setReferenceToken } from "../reference/session";

const LINK_RE = /^rr1\.[0-9a-f]{32}\.[0-9a-f]{32}\.[0-9a-f]{32}\.[A-Za-z0-9_-]{43}$/;
const bad = { ok: false as const, code: "invalid_input" };

const answersInput = z.record(
  z.string().regex(/^[a-z][a-z_]{0,31}$/),
  z.union([z.string().max(1000), z.number().int().min(1).max(5), z.null()]),
);

export type RefereeAnswers = z.infer<typeof answersInput>;

/** Use the signed link once: the session goes into its HTTP-only cookie. */
export async function openReference(token: string): Promise<ActionResult> {
  if (!LINK_RE.test(token)) return { ok: false, code: "not_found" };
  const result = await publicHireCall<{ session_token: string; expires_at: string }>("/v1/hire/public/references/open", {
    method: "POST",
    body: { token },
  });
  if (!result.ok) return { ok: false, code: result.code };
  await setReferenceToken(result.data.session_token, result.data.expires_at);
  return { ok: true };
}

export async function loadReference(locale: "en" | "ar"): Promise<ActionResult<{ page: HireRefereePage }>> {
  const result = await publicHireCall<HireRefereePage>(`/v1/hire/reference-session/me?locale=${locale === "ar" ? "ar" : "en"}`, {
    session: true,
  });
  return result.ok ? { ok: true, page: result.data } : { ok: false, code: result.code };
}

/** Save progress (`submit: false`) or submit the reference (every required answer). */
export async function saveReference(
  answers: RefereeAnswers,
  submit: boolean,
  locale: "en" | "ar",
): Promise<ActionResult<{ page: HireRefereePage }>> {
  const parsed = answersInput.safeParse(answers);
  if (!parsed.success) return bad;
  const query = `?locale=${locale === "ar" ? "ar" : "en"}`;
  const result = await publicHireCall<HireRefereePage>(
    submit ? `/v1/hire/reference-session/me/submit${query}` : `/v1/hire/reference-session/me${query}`,
    { method: submit ? "POST" : "PUT", body: { answers: parsed.data }, session: true },
  );
  return result.ok ? { ok: true, page: result.data } : { ok: false, code: result.code };
}
