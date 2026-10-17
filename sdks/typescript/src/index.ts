// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Proofly Technology

/**
 * `@pairly/sdk`: TypeScript client for the Pairly API (Node 20+ and edge runtimes).
 *
 * ```ts
 * import { Pairly, constructEvent } from "@pairly/sdk";
 * ```
 */

export {
  Pairly,
  type Approval,
  type ApprovalDetail,
  type ApprovalPage,
  type ApprovalQuery,
  type ApprovalStatus,
  type Batch,
  type BatchItem,
  type BatchOptions,
  type FileInput,
  type Proposal,
  type ProposalPage,
  type SubmitOptions,
  type Submission,
} from "./client.js";
export {
  BatchTimeoutError,
  PairlyAPIError,
  PairlyConfigError,
  PairlyConnectionError,
  PairlyError,
} from "./errors.js";
export { SDK_VERSION, retryDelay, type PairlyOptions, type RetryPolicy } from "./http.js";
export {
  DEFAULT_TOLERANCE_SECONDS,
  SIGNATURE_HEADER,
  WebhookSignatureError,
  buildEvent,
  computeSignature,
  constructEvent,
  parseEvent,
  serialise,
  signPayload,
  signedDelivery,
  verifySignature,
  verifyWebhookSignature,
  type ApprovalDecidedData,
  type BatchCompletedData,
  type DecisionRecordedData,
  type DocumentReceivedData,
  type EventType,
  type WebhookEvent,
} from "./webhooks.js";
