// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology

/**
 * Server-side reads of the public careers API (`/v1/hire/public/careers/...`). No token:
 * these routes are public. The visitor's address goes along in `X-Pairly-Client-IP`, so
 * the API's per-IP limit applies to each visitor rather than to this server.
 */
import "server-only";

import { headers } from "next/headers";

import { serverEnv } from "../env";
import { CLIENT_IP_HEADER, clientIpFrom, isSlug, type CareersOut, type Locale, type PublicJobDetail } from "./format";

export const CAREERS_TIMEOUT_MS = 5000;

export class CareersUnavailableError extends Error {
  constructor(status: number) {
    super(`careers API answered ${status}`);
    this.name = "CareersUnavailableError";
  }
}

async function getJson<T>(path: string): Promise<T | null> {
  const outgoing = new Headers({ Accept: "application/json" });
  const ip = clientIpFrom(await headers());
  if (ip) outgoing.set(CLIENT_IP_HEADER, ip);
  const response = await fetch(`${serverEnv().apiInternalUrl}${path}`, {
    headers: outgoing,
    // The API sends Cache-Control: public, max-age=300; the page is rendered per request.
    cache: "no-store",
    redirect: "error",
    signal: AbortSignal.timeout(CAREERS_TIMEOUT_MS),
  });
  if (response.status === 404) return null;
  if (!response.ok) throw new CareersUnavailableError(response.status);
  return (await response.json()) as T;
}

export async function fetchCareers(orgSlug: string, locale: Locale): Promise<CareersOut | null> {
  if (!isSlug(orgSlug)) return null;
  return getJson<CareersOut>(`/v1/hire/public/careers/${orgSlug}?locale=${locale}`);
}

export async function fetchJob(orgSlug: string, jobSlug: string, locale: Locale): Promise<PublicJobDetail | null> {
  if (!isSlug(orgSlug) || !isSlug(jobSlug)) return null;
  return getJson<PublicJobDetail>(`/v1/hire/public/careers/${orgSlug}/jobs/${jobSlug}?locale=${locale}`);
}
