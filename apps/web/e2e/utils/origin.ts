// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology

/**
 * The origin the suite opens. By default `http://localhost:<PAIRLY_WEB_PORT>` (3100): the
 * same host as `PAIRLY_PUBLIC_WEB_URL`, so magic links and invitations from Mailpit land
 * on the origin whose cookies and storage the browser already holds, and no extra trusted
 * origin is needed. The compose `web` service publishes that port on 127.0.0.1, which
 * `localhost` reaches. `PAIRLY_E2E_BASE_URL` overrides it (that origin must then be
 * `PAIRLY_PUBLIC_WEB_URL` or listed in `PAIRLY_AUTH_TRUSTED_ORIGINS`).
 */
export const WEB_PORT = process.env.PAIRLY_WEB_PORT ?? "3100";
export const BASE_URL = (process.env.PAIRLY_E2E_BASE_URL ?? `http://localhost:${WEB_PORT}`).replace(/\/$/, "");
/** The host in emailed links (`PAIRLY_PUBLIC_WEB_URL`'s host). */
export const EMAILED_HOST = process.env.PAIRLY_E2E_EMAILED_HOST ?? `localhost:${WEB_PORT}`;
