# @pairly/sdk (TypeScript SDK)

TypeScript client for the Pairly API: human-approved AI for hiring and accounts payable.
Typed from the OpenAPI spec (`@pairly/shared-ts` through `openapi-fetch`), retries with
backoff, pagination, idempotency keys and webhook verification. ESM and CommonJS
builds; Node 20+, Deno, Bun and edge runtimes (Web Crypto and `fetch` only, no Node
APIs). Version 0.1.0, AGPL-3.0-only (commercial license available, see `LICENSING.md`).

People decide in Pairly. An API key can read, propose and upload; it can never approve,
reject, pay or delete.

## Install

```sh
npm install @pairly/sdk                      # once published
# in this monorepo: "@pairly/sdk": "workspace:*", then
pnpm --filter @pairly/sdk build
```

## 10-minute quickstart

1. Start Pairly and get a sandbox key:

   ```sh
   make dev
   make sandbox NAME="My sandbox"           # prints a pk_test_ key once
   ```

2. Submit a document:

   ```ts
   import { readFile } from "node:fs/promises";
   import { Pairly } from "@pairly/sdk";

   const pairly = new Pairly({
     apiKey: process.env.PAIRLY_API_KEY!,
     baseUrl: process.env.PAIRLY_API_URL ?? "http://127.0.0.1:8000",
   });
   const job = await pairly.documents.submit(await readFile("invoice.pdf"), {
     workflow: "pay",
     filename: "invoice.pdf",
   });
   console.log(job.jobId, job.status, job.entityType, job.entityId);
   ```

   `submit` resolves at once with the job (a one-item batch) and what the workflow made
   of the document (Pairly Pay: `pay.invoice` and the invoice id). The AI reads, checks
   and routes it in the background; results arrive by webhook or polling
   (`pairly.documents.getBatch(job.jobId)`).

3. See what is waiting for a person:

   ```ts
   for (const approval of await pairly.approvals.listMine()) {
     console.log(approval.title, approval.sla_state, `http://localhost:3100/en/inbox/${approval.id}`);
   }
   ```

4. Suggest, do not decide:

   ```ts
   await pairly.proposals.create({
     entityType: "approval",
     entityId: approval.id,
     action: "approve",
     body: "Matches PO-1001 and the goods receipt.",
   });
   ```

5. Receive webhooks (verify the raw body first):

   ```ts
   import { constructEvent, WebhookSignatureError } from "@pairly/sdk";

   export async function POST(request: Request): Promise<Response> {
     const body = await request.text();
     try {
       const event = await constructEvent(body, request.headers.get("Pairly-Signature"), process.env.PAIRLY_WEBHOOK_SECRET!);
       if (event.type === "batch.completed") console.log(event.data.counts);
     } catch (error) {
       if (error instanceof WebhookSignatureError) return new Response("bad signature", { status: 400 });
       throw error;
     }
     return new Response(null, { status: 204 });
   }
   ```

   `docs/quickstarts/` has Next.js and Express apps (and FastAPI and Flask with the
   Python SDK).

## Reference

| Call | API |
|---|---|
| `documents.submit(file, { workflow, filename?, metadata? })` | `POST /v1/documents/batch` (one item) |
| `documents.submitBatch({ workflow, files?, urls?, metadata? })` | `POST /v1/documents/batch` (up to 50) |
| `documents.getBatch(id)`, `documents.waitForBatch(id)` | `GET /v1/batches/{id}` |
| `approvals.list(q)`, `approvals.iterate(q)`, `approvals.listMine()` | `GET /v1/approvals` |
| `approvals.get(id)` | `GET /v1/approvals/{id}` |
| `proposals.create(p)`, `proposals.list(q)` | `/v1/proposals` |
| `decisions.receipt(decId)` | `GET /v1/decisions/{id}/receipt` |
| `api.GET(...)`, `api.POST(...)` | every route, typed by `openapi-fetch` |
| `request(method, path, init)`, `paginate(path, query)` | raw calls with the same retries |

* **Credentials**: `apiKey`, or `token` plus `orgId` (a person's JWT). `baseUrl` must be
  https unless it is this machine.
* **Retries**: 429 (honouring `Retry-After`), 500, 502, 503, 504 and network errors, up to
  `maxRetries` (3), capped exponential backoff with full jitter. Every POST gets an
  `Idempotency-Key` that stays the same across its retries (`idempotencyKey` to set your
  own).
* **Errors**: `PairlyAPIError` (`status`, `code`, `detail`, `requestId`, `retryAfterMs`),
  `PairlyConnectionError`, `PairlyConfigError`, `BatchTimeoutError`.
* **Webhooks**: `verifySignature`, `constructEvent`, `parseEvent`, typed event data
  (narrow on `event.type`), and `buildEvent` and `signedDelivery` to test a handler with
  the bytes and `Pairly-Signature` Pairly sends. Tested against a vector produced by the
  engine itself.

## Build

```sh
pnpm --filter @pairly/sdk build      # dist/esm and dist/cjs, each with .d.ts
pnpm vitest run --project libs       # the SDK's tests
```

Before publishing to npm, `@pairly/shared-ts` (the generated types this package's
declarations import) must be published too, or its types inlined.
