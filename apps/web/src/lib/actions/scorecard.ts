// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
"use server";

/**
 * Server actions of the interviewer's scorecard page (M3). The browser never sees the
 * scorecard session: it lives in an HTTP-only cookie (`../scorecard/session`). The API
 * decides everything, including that others' feedback stays hidden until you submit.
 */
import { z } from "zod";

import type { ActionResult } from "../action-result";
import type { HireScorecardPage } from "../hire/types";
import { scorecardCall, setScorecardToken } from "../scorecard/session";

const LINK_RE = /^s[kl]1\.[0-9a-f]{32}\.[0-9a-f]{32}\.[0-9a-f]{32}\.[A-Za-z0-9_-]{43}$/;
const bad = { ok: false as const, code: "invalid_input" };

const scorecardInput = z.object({
  ratings: z.record(z.string().regex(/^[a-z][a-z0-9_]{0,47}$/), z.number().int().min(1).max(4)),
  notes: z.string().max(1000),
  recommendation: z.enum(["strong_no", "no", "yes", "strong_yes"]).nullable(),
});

export type ScorecardInput = z.infer<typeof scorecardInput>;

/** Use the one-click link once: the session goes into its HTTP-only cookie. */
export async function openScorecard(token: string): Promise<ActionResult> {
  if (!LINK_RE.test(token) || !token.startsWith("sk1.")) return { ok: false, code: "not_found" };
  const result = await scorecardCall<{ session_token: string; expires_at: string }>("/v1/hire/public/scorecards/open", {
    method: "POST",
    body: { token },
    session: false,
  });
  if (!result.ok) return { ok: false, code: result.code };
  await setScorecardToken(result.data.session_token, result.data.expires_at);
  return { ok: true };
}

/** "Remind me later" from a reminder email: one click, single use. */
export async function snoozeFromLink(token: string): Promise<ActionResult<{ until: string }>> {
  if (!LINK_RE.test(token) || !token.startsWith("sl1.")) return { ok: false, code: "not_found" };
  const result = await scorecardCall<{ snoozed_until: string }>("/v1/hire/public/scorecards/later", {
    method: "POST",
    body: { token },
    session: false,
  });
  return result.ok ? { ok: true, until: result.data.snoozed_until } : { ok: false, code: result.code };
}

export async function loadScorecard(locale: "en" | "ar"): Promise<ActionResult<{ page: HireScorecardPage }>> {
  const result = await scorecardCall<HireScorecardPage>(`/v1/hire/scorecard-session/me?locale=${locale === "ar" ? "ar" : "en"}`);
  return result.ok ? { ok: true, page: result.data } : { ok: false, code: result.code };
}

export async function saveScorecard(
  input: ScorecardInput,
  submit: boolean,
): Promise<ActionResult<{ page: HireScorecardPage }>> {
  const parsed = scorecardInput.safeParse(input);
  if (!parsed.success) return bad;
  const result = await scorecardCall<HireScorecardPage>(
    submit ? "/v1/hire/scorecard-session/me/submit" : "/v1/hire/scorecard-session/me",
    { method: submit ? "POST" : "PUT", body: parsed.data },
  );
  return result.ok ? { ok: true, page: result.data } : { ok: false, code: result.code };
}

export async function snoozeScorecard(): Promise<ActionResult<{ until: string }>> {
  const result = await scorecardCall<{ snoozed_until: string }>("/v1/hire/scorecard-session/me/later", {
    method: "POST",
  });
  return result.ok ? { ok: true, until: result.data.snoozed_until } : { ok: false, code: result.code };
}
