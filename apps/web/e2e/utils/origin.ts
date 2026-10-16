// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology

/**
 * The origin the suite opens. By default `http://localhost:<LOCAL_WEB_PORT or PAIRLY_WEB_PORT>`
 * (3100): the
 * same host as `PAIRLY_PUBLIC_WEB_URL`, so magic links and invitations from Mailpit land
 * on the origin whose cookies and storage the browser already holds, and no extra trusted
 * origin is needed. The compose `web` service publishes that port on 127.0.0.1, which
 * `localhost` reaches. `PAIRLY_E2E_BASE_URL` overrides it (that origin must then be
 * `PAIRLY_PUBLIC_WEB_URL` or listed in `PAIRLY_AUTH_TRUSTED_ORIGINS`).
 */
export const WEB_PORT = process.env.LOCAL_WEB_PORT ?? process.env.PAIRLY_WEB_PORT ?? "3100";
/**
 * The API port of the stack under test: `LOCAL_API_PORT` or `PAIRLY_API_PORT` (8000). Set
 * the same variables for `make local-up` and for the suite: `scripts/local_lib.sh` then
 * derives the public URLs, the auth issuer and audience and the CORS origin from them,
 * whatever the repo-root `.env` (or a worktree's generated one) says.
 */
export const API_PORT = process.env.LOCAL_API_PORT ?? process.env.PAIRLY_API_PORT ?? "8000";
export const BASE_URL = (process.env.PAIRLY_E2E_BASE_URL ?? `http://localhost:${WEB_PORT}`).replace(/\/$/, "");
/** The host in emailed links (`PAIRLY_PUBLIC_WEB_URL`'s host). */
export const EMAILED_HOST = process.env.PAIRLY_E2E_EMAILED_HOST ?? `localhost:${WEB_PORT}`;
