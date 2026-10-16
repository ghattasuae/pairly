// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology

/**
 * `Pairly`: the TypeScript client. Typed by the OpenAPI types in `@pairly/shared-ts`
 * (through `openapi-fetch`), with one-call helpers on top.
 *
 * ```ts
 * import { Pairly } from "@pairly/sdk";
 *
 * const pairly = new Pairly({ apiKey: process.env.PAIRLY_API_KEY });
 * const job = await pairly.documents.submit(file, { workflow: "pay" });
 * const waiting = await pairly.approvals.listMine();
 * ```
 *
 * `pairly.api` is the full typed client for every other route
 * (`pairly.api.GET("/v1/pay/invoices/{invoice_id}", { params: { path: { invoice_id } } })`).
 * People decide in Pairly: an API key can read, propose and upload, never approve.
 */

import createClient, { type Client } from "openapi-fetch";
import type { components, paths } from "@pairly/shared-ts";

import { BatchTimeoutError } from "./errors.js";
import {
  type PairlyOptions,
  type ResolvedConfig,
  decode,
  problem,
  resolveConfig,
  retryingFetch,
  uuidSegment,
} from "./http.js";

export type Batch = components["schemas"]["BatchOut"];
export type BatchItem = components["schemas"]["BatchItemOut"];
export type Approval = components["schemas"]["ApprovalOut"];
export type ApprovalPage = components["schemas"]["InboxOut"];
export type ApprovalDetail = components["schemas"]["ApprovalDetailOut"];
export type Proposal = components["schemas"]["ProposalOut"];
export type ProposalPage = components["schemas"]["ProposalList"];
export type ApprovalStatus = NonNullable<
  NonNullable<paths["/v1/approvals"]["get"]["parameters"]["query"]>["status"]
>;

/** A file: a Blob or File (browsers, edge, Node 20+), or raw bytes. */
export type FileInput = Blob | Uint8Array | ArrayBuffer;

/** What `documents.submit` returns: the job (a one-item batch) and its item. */
export interface Submission {
  /** The batch id: poll `documents.getBatch(jobId)` or wait for `batch.completed`. */
  jobId: string;
  status: BatchItem["status"];
  documentId: string | null;
  /** What the workflow made of the document (Pairly Pay: `pay.invoice` and its id). */
  entityType: string | null;
  entityId: string | null;
  item: BatchItem;
  batch: Batch;
}

type RequestBody = NonNullable<RequestInit["body"]>;

const DECISION_ID_RE = /^dec_[0-9A-HJKMNP-TV-Z]{26}$/;

function toBlob(file: FileInput): Blob {
  if (file instanceof Blob) return file;
  const bytes = file instanceof Uint8Array ? file : new Uint8Array(file);
  // Copy into a plain ArrayBuffer-backed view (a SharedArrayBuffer cannot be a Blob part).
  return new Blob([new Uint8Array(bytes)]);
}

function submission(batch: Batch): Submission {
  const item = batch.items[0];
  if (item === undefined) throw new TypeError("the batch has no item");
  return {
    jobId: batch.id,
    status: item.status,
    documentId: item.document_id ?? null,
    entityType: item.entity_type ?? null,
    entityId: item.entity_id ?? null,
    item,
    batch,
  };
}

async function unwrap<T>(result: { data?: T; error?: unknown; response: Response }): Promise<T> {
  if (result.response.status >= 400) throw problem(result.response, result.error ?? null);
  return result.data as T;
}

export interface SubmitOptions {
  workflow: string;
  filename?: string;
  metadata?: Record<string, unknown>;
  idempotencyKey?: string;
}

export interface BatchOptions {
  workflow: string;
  files?: Array<FileInput | { file: FileInput; filename?: string }>;
  urls?: string[];
  metadata?: Record<string, unknown>;
  idempotencyKey?: string;
}

export interface ApprovalQuery {
  workflow?: string;
  status?: ApprovalStatus;
  assignedToMe?: boolean;
  limit?: number;
  cursor?: string;
}

export class Pairly {
  /** The typed `openapi-fetch` client for every route, with the same retries. */
  readonly api: Client<paths>;
  readonly config: ResolvedConfig;
  readonly documents: Documents;
  readonly approvals: Approvals;
  readonly proposals: Proposals;
  readonly decisions: Decisions;
  private readonly send: (input: Request) => Promise<Response>;

  constructor(options: PairlyOptions) {
    this.config = resolveConfig(options);
    this.send = retryingFetch(this.config);
    this.api = createClient<paths>({ baseUrl: this.config.baseUrl, fetch: this.send });
    this.documents = new Documents(this);
    this.approvals = new Approvals(this);
    this.proposals = new Proposals(this);
    this.decisions = new Decisions(this);
  }

  /** A raw call with the client's credentials and retries (`path` starts with `/v1/`). */
  async request<T = unknown>(
    method: string,
    path: string,
    init: { query?: Record<string, string | number | boolean | undefined>; json?: unknown; body?: RequestBody; headers?: Record<string, string> } = {},
  ): Promise<T> {
    if (!path.startsWith("/v1/")) throw new TypeError("path must start with /v1/");
    const url = new URL(this.config.baseUrl + path);
    for (const [key, value] of Object.entries(init.query ?? {})) {
      if (value !== undefined) url.searchParams.set(key, String(value));
    }
    const headers = new Headers(init.headers);
    let body: RequestBody | null = init.body ?? null;
    if (init.json !== undefined) {
      headers.set("Content-Type", "application/json");
      body = JSON.stringify(init.json);
    }
    return decode<T>(await this.send(new Request(url, { method: method.toUpperCase(), headers, body })));
  }

  /** Every item of a cursor-paged list (`next_cursor` / `cursor`). */
  async *paginate<T>(path: string, query: Record<string, string | number | boolean | undefined> = {}): AsyncGenerator<T> {
    let cursor: string | undefined;
    for (;;) {
      const page = await this.request<{ items: T[]; next_cursor: string | null }>("GET", path, {
        query: { ...query, cursor },
      });
      yield* page.items;
      if (!page.next_cursor) return;
      cursor = page.next_cursor;
    }
  }
}

class Documents {
  constructor(private readonly client: Pairly) {}

  /**
   * Submit one document to a workflow. Resolves to the job (a one-item batch) and what
   * the workflow made of the document. Decisions arrive later by webhook
   * (`decision.recorded`, `approval.decided`) or polling.
   */
  async submit(file: FileInput, options: SubmitOptions): Promise<Submission> {
    const { filename, ...rest } = options;
    const entry = filename === undefined ? { file } : { file, filename };
    return submission(await this.submitBatch({ ...rest, files: [entry] }));
  }

  /** Up to 50 files and URLs in one request; `batch.completed` fires when done. */
  async submitBatch(options: BatchOptions): Promise<Batch> {
    const files = options.files ?? [];
    const urls = options.urls ?? [];
    if (files.length === 0 && urls.length === 0) throw new TypeError("give at least one file or URL");
    const headers: Record<string, string> = {};
    if (options.idempotencyKey) headers["Idempotency-Key"] = options.idempotencyKey;
    const query = { workflow: options.workflow };
    if (files.length === 0) {
      const json: Record<string, unknown> = { urls };
      if (options.metadata !== undefined) json["metadata"] = options.metadata;
      return this.client.request<Batch>("POST", "/v1/documents/batch", { query, json, headers });
    }
    const form = new FormData();
    files.forEach((entry, index) => {
      const { file, filename } =
        entry instanceof Blob || entry instanceof Uint8Array || entry instanceof ArrayBuffer
          ? { file: entry, filename: undefined }
          : entry;
      const name = filename ?? (file instanceof File ? file.name : `document-${index + 1}`);
      form.append("file", toBlob(file), name);
    });
    if (urls.length > 0) form.append("urls", JSON.stringify(urls));
    if (options.metadata !== undefined) form.append("metadata", JSON.stringify(options.metadata));
    return this.client.request<Batch>("POST", "/v1/documents/batch", { query, body: form, headers });
  }

  async getBatch(batchId: string): Promise<Batch> {
    const batch_id = uuidSegment(batchId, "batchId");
    return unwrap(await this.client.api.GET("/v1/batches/{batch_id}", { params: { path: { batch_id } } }));
  }

  /** Poll until the batch is completed (or reject with `BatchTimeoutError`). */
  async waitForBatch(batchId: string, options: { timeoutMs?: number; intervalMs?: number } = {}): Promise<Batch> {
    const deadline = Date.now() + (options.timeoutMs ?? 120_000);
    for (;;) {
      const batch = await this.getBatch(batchId);
      if (batch.status === "completed") return batch;
      if (Date.now() >= deadline) throw new BatchTimeoutError(`batch ${batchId} is still ${batch.status}`);
      await this.client.config.sleep(options.intervalMs ?? 1000);
    }
  }
}

class Approvals {
  constructor(private readonly client: Pairly) {}

  async list(query: ApprovalQuery = {}): Promise<ApprovalPage> {
    return unwrap(
      await this.client.api.GET("/v1/approvals", {
        params: {
          query: {
            ...(query.workflow !== undefined && { workflow: query.workflow }),
            ...(query.status !== undefined && { status: query.status }),
            ...(query.assignedToMe === true && { assigned_to_me: true }),
            limit: query.limit ?? 50,
            ...(query.cursor !== undefined && { cursor: query.cursor }),
          },
        },
      }),
    );
  }

  /** Every approval matching `query`, across pages. */
  async *iterate(query: Omit<ApprovalQuery, "cursor" | "limit"> = {}): AsyncGenerator<Approval> {
    let cursor: string | undefined;
    for (;;) {
      const page = await this.list({ ...query, limit: 200, ...(cursor !== undefined && { cursor }) });
      yield* page.items;
      if (!page.next_cursor) return;
      cursor = page.next_cursor;
    }
  }

  /** Every approval waiting for me (the person, or the API key's creator). */
  async listMine(query: { workflow?: string; status?: ApprovalStatus } = {}): Promise<Approval[]> {
    const out: Approval[] = [];
    for await (const approval of this.iterate({ status: "pending", ...query, assignedToMe: true })) {
      out.push(approval);
    }
    return out;
  }

  async get(approvalId: string): Promise<ApprovalDetail> {
    const approval_id = uuidSegment(approvalId, "approvalId");
    return unwrap(await this.client.api.GET("/v1/approvals/{approval_id}", { params: { path: { approval_id } } }));
  }
}

class Proposals {
  constructor(private readonly client: Pairly) {}

  /** Suggest an action (or leave a note) for a person to decide. Changes nothing. */
  async create(proposal: {
    entityType: string;
    entityId: string;
    body: string;
    action?: string;
    workflow?: string;
  }): Promise<Proposal> {
    return unwrap(
      await this.client.api.POST("/v1/proposals", {
        body: {
          entity_type: proposal.entityType,
          entity_id: proposal.entityId,
          kind: proposal.action ? "action" : "note",
          body: proposal.body,
          ...(proposal.action && { action: proposal.action }),
          ...(proposal.workflow && { workflow: proposal.workflow }),
        },
      }),
    );
  }

  async list(query: { entityType?: string; entityId?: string; workflow?: string; limit?: number; cursor?: string } = {}): Promise<ProposalPage> {
    return unwrap(
      await this.client.api.GET("/v1/proposals", {
        params: {
          query: {
            ...(query.entityType !== undefined && { entity_type: query.entityType }),
            ...(query.entityId !== undefined && { entity_id: query.entityId }),
            ...(query.workflow !== undefined && { workflow: query.workflow }),
            limit: query.limit ?? 50,
            ...(query.cursor !== undefined && { cursor: query.cursor }),
          },
        },
      }),
    );
  }
}

class Decisions {
  constructor(private readonly client: Pairly) {}

  /** The receipt of one decision record (`dec_...`). */
  async receipt(decisionId: string): Promise<Record<string, unknown>> {
    if (!DECISION_ID_RE.test(decisionId)) throw new TypeError("decisionId must look like dec_ followed by 26 characters");
    return this.client.request<Record<string, unknown>>("GET", `/v1/decisions/${decisionId}/receipt`);
  }
}
