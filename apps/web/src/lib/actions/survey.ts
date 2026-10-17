// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
"use server";

/**
 * Server actions of the candidate experience survey (M3, brief 3.8.5). The signed link
 * stays in the page's fragment; these actions pass it to the API in a JSON body. Reading
 * the form never uses the link; answering uses it once. Anonymous unless the candidate
 * chooses to attach their name.
 */
import { z } from "zod";

import type { ActionResult } from "../action-result";
import type { HireSurveyForm } from "../hire/types";
import { publicHireCall } from "../reference/session";

const LINK_RE = /^sv1\.[0-9a-f]{32}\.[0-9a-f]{32}\.[A-Za-z0-9_-]{43}$/;
const bad = { ok: false as const, code: "invalid_input" };

export async function viewSurvey(token: string, locale: "en" | "ar"): Promise<ActionResult<{ form: HireSurveyForm }>> {
  if (!LINK_RE.test(token)) return { ok: false, code: "not_found" };
  const result = await publicHireCall<HireSurveyForm>("/v1/hire/public/surveys/view", {
    method: "POST",
    body: { token, locale: locale === "ar" ? "ar" : "en" },
  });
  return result.ok ? { ok: true, form: result.data } : { ok: false, code: result.code };
}

const answersInput = z.object({
  overall: z.number().int().min(1).max(5),
  communication: z.number().int().min(1).max(5).nullable(),
  respect: z.number().int().min(1).max(5).nullable(),
  recommend: z.enum(["yes", "no", "unsure"]).nullable(),
  comment: z.string().max(1000),
});

export type SurveyAnswers = z.infer<typeof answersInput>;

export async function answerSurvey(token: string, answers: SurveyAnswers, attachName: boolean): Promise<ActionResult> {
  if (!LINK_RE.test(token)) return { ok: false, code: "not_found" };
  const parsed = answersInput.safeParse(answers);
  if (!parsed.success || typeof attachName !== "boolean") return bad;
  const body = Object.fromEntries(Object.entries(parsed.data).filter(([, v]) => v !== null && v !== ""));
  const result = await publicHireCall<{ ok: boolean }>("/v1/hire/public/surveys/answer", {
    method: "POST",
    body: { token, answers: body, attach_name: attachName },
  });
  return result.ok ? { ok: true } : { ok: false, code: result.code };
}
