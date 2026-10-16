# pairly (Python SDK)

Python client for the Pairly API: human-approved AI for hiring and accounts payable.
Typed, sync and async, retries with backoff, pagination helpers, idempotency keys and
webhook verification. Version 0.1.0, AGPL-3.0-only (commercial license available, see
`LICENSING.md` at the repository root).

People decide in Pairly. An API key can read, propose and upload; it can never approve,
reject, pay or delete.

## Install

```sh
pip install pairly            # from PyPI, once published
# from this repository:
pip install ./sdks/python     # or: uv add --editable ./sdks/python
```

Python 3.12 or later. One dependency: `httpx`.

## 10-minute quickstart

1. Start Pairly and get a sandbox key (native stack, no Docker):

   ```sh
   make dev
   make sandbox NAME="My sandbox"    # prints a pk_test_ key once
   export PAIRLY_API_KEY=pk_test_...
   export PAIRLY_API_URL=http://127.0.0.1:8000
   ```

2. Submit a document and see what Pairly made of it:

   ```python
   from pairly import Pairly

   pairly = Pairly()  # reads PAIRLY_API_KEY and PAIRLY_API_URL
   job = pairly.documents.submit("invoice.pdf", workflow="pay")
   print(job.job_id, job.status, job.entity_type, job.entity_id)
   # 5f0c... accepted pay.invoice 8a1e...
   ```

   `submit` returns at once. The AI reads, checks and routes the invoice in the
   background; results arrive by webhook (`decision.recorded`, `approval.decided`) or by
   polling (`pairly.documents.get_batch(job.job_id)`).

3. See what is waiting for a person:

   ```python
   for approval in pairly.approvals.list_mine():
       print(approval["title"], approval["sla_state"])
       print(f"  decide: http://localhost:3100/en/inbox/{approval['id']}")
   ```

   A key with the `core.approval.read` scope lists the inbox of the person who created
   it, read only.

4. Suggest, do not decide:

   ```python
   pairly.proposals.create(
       "approval", approval["id"], "Matches PO-1001 and the receipt.", action="approve"
   )
   ```

5. Receive webhooks. Register an endpoint in Pairly (Settings, Webhooks), keep its
   `whsec_...` secret, and verify every delivery with the raw body:

   ```python
   from pairly.webhooks import WebhookSignatureError, construct_event


   def handle(body: bytes, signature: str | None) -> None:
       try:
           event = construct_event(body, signature, secret="whsec_...")
       except WebhookSignatureError:
           return  # answer 400
       if event.type == "batch.completed":
           done = event.batch_completed()
           print(done["batch_id"], done["counts"])
   ```

   `docs/quickstarts/` has the same flow as FastAPI, Flask, Next.js and Express apps.

## Async

```python
import asyncio
from pairly import AsyncPairly


async def main() -> None:
    async with AsyncPairly() as pairly:
        job = await pairly.documents.submit("invoice.pdf", workflow="pay")
        mine = await pairly.approvals.list_mine()


asyncio.run(main())
```

## Reference

| Call | API |
|---|---|
| `documents.submit(file, workflow=, filename=, metadata=)` | `POST /v1/documents/batch` (one item) |
| `documents.submit_batch(files=, urls=, workflow=, metadata=)` | `POST /v1/documents/batch` (up to 50) |
| `documents.get_batch(id)`, `documents.wait_for_batch(id)` | `GET /v1/batches/{id}` |
| `documents.get(id)` | `GET /v1/documents/{id}` |
| `approvals.list(...)`, `approvals.iter(...)`, `approvals.list_mine()` | `GET /v1/approvals` |
| `approvals.get(id)` | `GET /v1/approvals/{id}` |
| `proposals.create(...)`, `proposals.list(...)` | `/v1/proposals` |
| `decisions.receipt(dec_id)` | `GET /v1/decisions/{id}/receipt` |
| `request(method, path, ...)`, `paginate(path, params)` | anything else |

* **Credentials**: `api_key=` (or `PAIRLY_API_KEY`), or `token=` plus `org_id=` (a
  person's JWT, or `PAIRLY_TOKEN` and `PAIRLY_ORG`). `base_url=` (or `PAIRLY_API_URL`)
  must be https, except for this machine.
* **Retries**: 429 (honouring `Retry-After`), 500, 502, 503, 504 and connection errors,
  up to `max_retries=3`, exponential backoff with full jitter (`RetryPolicy`). Every POST
  carries an `Idempotency-Key` that stays the same across its retries; pass your own with
  `idempotency_key=` to make a retry after a crash safe too.
* **Errors**: `PairlyAPIError` with `status`, `code`, `detail`, `request_id`;
  subclasses `BadRequestError`, `AuthenticationError`, `PermissionDeniedError`,
  `NotFoundError`, `ConflictError`, `RateLimitError` (`retry_after`), `ServerError`.
* **Webhooks** (`pairly.webhooks`): `verify_signature`, `construct_event`, `parse_event`,
  typed data (`event.batch_completed()` and friends), and `build_event`,
  `signed_delivery` and the `*_event` constructors to test your handler with exactly
  the bytes and `Pairly-Signature` Pairly sends. Standard library only, mirroring the
  engine's signing (`HMAC-SHA256(secret, "<t>." + body)`, 5 minute tolerance).

## Why a hand-written client

The API has over 300 routes; a generated client (openapi-python-client) would ship them
all as hundreds of modules and models, add a code generator to the toolchain, and still
need the one-call helpers, retries and webhook code written by hand. This package is a
small typed layer over httpx instead: one `Call` description runs on both the sync and
the async client, responses are `TypedDict`s (plain dicts at run time, so new fields
never break you), and `request()` reaches every other route. It stays tied to the spec
by a test: `tests/test_openapi_conformance.py` checks every route it calls and every key
of its typed responses against `packages/shared-ts/openapi/v1.json`.
