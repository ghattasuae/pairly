# Pairly API: Bruno collection

A [Bruno](https://www.usebruno.com) collection for the main endpoints: health, documents,
batches, approvals, proposals, decision records, Pay, Hire, search, webhooks and account
(API keys, audit).

1. Open this folder in Bruno (Open Collection, `infra/bruno`).
2. Pick the `native` environment (the native stack, `make dev`; `docker` for
   `make docker-dev`).
3. Set the secrets in the environment (they stay on your machine, never in git):
   * `apiKey`: a key, for example the `pk_test_` key `make sandbox` prints;
   * `token` and `orgId`: a person's JWT and organisation, for the requests marked
     "person" (deciding, webhooks, API keys, audit).

Requests chain: uploading or listing sets `documentId`, `batchId`, `approvalId`,
`decisionId` and `invoiceId` for the next ones. POST requests send a fresh
`Idempotency-Key`. `sample-invoice.pdf` is synthetic.

People decide in Pairly: "Decide (person only)" needs a person's JWT with a TOTP sign-in,
and an API key gets 403 there. `scripts/tests/test_bruno_collection.py` checks that every
request matches a route in the OpenAPI document and that no secret is committed. The
generated reference: `make api-docs` (dist/api-docs/index.html).
