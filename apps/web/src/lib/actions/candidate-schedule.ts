// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
"use server";

/**
 * Server actions for a candidate's own interview scheduling page. No sign-in: the signed
 * `hs1` token from their email is the proof, and the API decides everything. The token
 * travels in the JSON body, never in a URL.
 */
import { headers } from "next/headers";

import { getPublicApiClient } from "../api/server";
import { CLIENT_IP_HEADER, clientIpFrom } from "../careers/format";
import type { PublicSchedule } from "../hire/scheduling";

const TOKEN_RE = /^hs1\.[0-9a-f]{32}\.[0-9a-f]{32}\.[0-9a-f]{32}\.[a-z][a-z0-9_]{0,31}\.[0-9a-f]{16}\.\d{1,12}\.[A-Za-z0-9_-]{43}$/;
const TZ_RE = /^[A-Za-z0-9_+\-/]{1,64}$/;
const ISO_RE = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:\d{2})$/;

export type ScheduleView =
  | { kind: "ready"; schedule: PublicSchedule }
  | { kind: "invalid" }
  | { kind: "used" }
  | { kind: "taken" }
  | { kind: "closed" }
  | { kind: "unavailable" };

async function forwarded(): Promise<Record<string, string>> {
  const ip = clientIpFrom(await headers());
  return ip ? { [CLIENT_IP_HEADER]: ip } : {};
}

function problem(error: unknown): string | null {
  return error && typeof error === "object" && "code" in error && typeof error.code === "string" ? error.code : null;
}

function view(status: number, data: PublicSchedule | undefined, error: unknown): ScheduleView {
  if (status === 200 && data) return { kind: "ready", schedule: data };
  if (status === 404) return { kind: "invalid" };
  if (status === 410) return { kind: "used" };
  if (status === 409) return problem(error) === "slot_taken" ? { kind: "taken" } : { kind: "closed" };
  return { kind: "unavailable" };
}

export async function readSchedule(token: string, timezone: string): Promise<ScheduleView> {
  if (!TOKEN_RE.test(token)) return { kind: "invalid" };
  try {
    const { data, error, response } = await getPublicApiClient().POST("/v1/hire/public/scheduling/read", {
      body: { token, timezone: TZ_RE.test(timezone) ? timezone : null },
      headers: await forwarded(),
    });
    return view(response.status, data, error);
  } catch {
    return { kind: "unavailable" };
  }
}

export async function bookSchedule(token: string, startsAt: string, timezone: string): Promise<ScheduleView> {
  if (!TOKEN_RE.test(token) || !ISO_RE.test(startsAt)) return { kind: "invalid" };
  try {
    const { data, error, response } = await getPublicApiClient().POST("/v1/hire/public/scheduling/book", {
      body: { token, starts_at: startsAt, timezone: TZ_RE.test(timezone) ? timezone : null },
      headers: await forwarded(),
    });
    return view(response.status, data, error);
  } catch {
    return { kind: "unavailable" };
  }
}

export async function cancelSchedule(token: string, timezone: string): Promise<ScheduleView> {
  if (!TOKEN_RE.test(token)) return { kind: "invalid" };
  try {
    const { data, error, response } = await getPublicApiClient().POST("/v1/hire/public/scheduling/cancel", {
      body: { token, timezone: TZ_RE.test(timezone) ? timezone : null },
      headers: await forwarded(),
    });
    return view(response.status, data, error);
  } catch {
    return { kind: "unavailable" };
  }
}
