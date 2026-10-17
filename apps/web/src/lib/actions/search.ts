// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
"use server";

/** Global search for the command palette (`POST /v1/search`). Kinds the actor cannot search are skipped by the API. */
import { z } from "zod";

import type { ActionResult } from "../action-result";
import { callApi, toAction } from "../api/call";
import type { SearchHit } from "../api/types";

const querySchema = z.string().trim().min(2).max(200);

export async function searchEverything(query: string): Promise<ActionResult<{ hits: SearchHit[] }>> {
  const parsed = querySchema.safeParse(query);
  if (!parsed.success) return { ok: false, code: "invalid_input" };
  const result = await callApi((api) => api.POST("/v1/search", { body: { query: parsed.data, limit: 8 } }));
  return toAction(result, (hits) => ({ hits }));
}
