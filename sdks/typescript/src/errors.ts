// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology

/** Errors raised by the Pairly client. API errors carry RFC 9457 problem details. */

export class PairlyError extends Error {
  override name = "PairlyError";
}

/** The client is not configured (no credential, a bad URL). */
export class PairlyConfigError extends PairlyError {
  override name = "PairlyConfigError";
}

/** The API could not be reached, after the retries. */
export class PairlyConnectionError extends PairlyError {
  override name = "PairlyConnectionError";
}

export interface ProblemFields {
  status: number;
  title: string;
  detail: string;
  code: string | null;
  requestId: string | null;
  retryAfterMs: number | null;
  body: unknown;
}

/** The API answered with an error status. `code` is the problem's machine-readable code. */
export class PairlyAPIError extends PairlyError {
  override name = "PairlyAPIError";
  readonly status: number;
  readonly title: string;
  readonly detail: string;
  readonly code: string | null;
  readonly requestId: string | null;
  readonly retryAfterMs: number | null;
  readonly body: unknown;

  constructor(fields: ProblemFields) {
    super(fields.detail ? `${fields.status} ${fields.title}: ${fields.detail}` : `${fields.status} ${fields.title}`);
    this.status = fields.status;
    this.title = fields.title;
    this.detail = fields.detail;
    this.code = fields.code;
    this.requestId = fields.requestId;
    this.retryAfterMs = fields.retryAfterMs;
    this.body = fields.body;
  }

  /** 403: the credential may not do this (API keys never approve or decide). */
  get isPermissionDenied(): boolean {
    return this.status === 403;
  }

  /** 429: too many requests; see `retryAfterMs`. */
  get isRateLimited(): boolean {
    return this.status === 429;
  }
}

/** `waitForBatch` gave up before the batch completed. */
export class BatchTimeoutError extends PairlyError {
  override name = "BatchTimeoutError";
}
