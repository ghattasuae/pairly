# Add human-approved AI to your app in 10 minutes

Four small apps do the same three things with the Pairly SDKs:

1. **submit a document**: an invoice goes to Pairly Pay (`documents.submit`), whose AI
   reads it, checks it (duplicates, bank details, PO match, hidden instructions) and
   routes it for approval;
2. **receive the webhook**: Pairly calls your app when the batch is done and when a
   person decides (`approval.decided`), signed with your endpoint's secret;
3. **show the approval link**: your app lists what is waiting and links each item to
   Pairly, where a person decides. Your app never approves anything: API keys cannot.

| Example | SDK | Run it |
|---|---|---|
| [`fastapi/app.py`](fastapi/app.py) | `pairly` (async) | `uvicorn app:app --port 5001` |
| [`flask/app.py`](flask/app.py) | `pairly` (sync) | `flask --app app run --port 5002` |
| [`nextjs/`](nextjs/) | `@pairly/sdk` (server action, edge route) | `pnpm --filter @pairly/quickstart-nextjs dev` |
| [`express/server.ts`](express/server.ts) | `@pairly/sdk` | `PORT=5003 node --experimental-strip-types server.ts` |

## 1. Run Pairly with a sandbox (3 minutes)

Natively, no Docker:

```sh
make dev
make sandbox NAME="Quickstart" ADMIN_EMAIL=you@example.test
```

`make sandbox` prints a `pk_test_` API key (once) and an invitation link for you. The
sandbox already has two invoices waiting for approval. Export what the apps read:

```sh
export PAIRLY_API_KEY=pk_test_...
export PAIRLY_API_URL=http://127.0.0.1:8000
export PAIRLY_WEB_URL=http://localhost:3100
```

## 2. Register your webhook (2 minutes)

Accept the invitation, then in Pairly open **Settings, Webhooks** and add your app's URL
(`http://localhost:5001/webhooks/pairly` for FastAPI, `.../api/pairly/webhook` for
Next.js) with the events `batch.completed`, `approval.decided` and `decision.recorded`.
Copy the signing secret:

```sh
export PAIRLY_WEBHOOK_SECRET=whsec_...
```

Pairly only calls public https URLs. For a local app in development, add
`PAIRLY_WEBHOOK_ALLOW_HTTP=true` and `PAIRLY_WEBHOOK_ALLOW_PRIVATE_TARGETS=true` to `.env`
and restart (`make down && make dev`); both are refused outside development.

## 3. Run an example (5 minutes)

```sh
# Python (from the repository root)
uv run --with uvicorn uvicorn --app-dir docs/quickstarts/fastapi app:app --port 5001
uv run flask --app docs/quickstarts/flask/app run --port 5002

# TypeScript
pnpm --filter @pairly/sdk build
pnpm --filter @pairly/quickstart-nextjs dev
(cd docs/quickstarts/express && PORT=5003 node --experimental-strip-types server.ts)
```

Open the app, submit a synthetic invoice
(`uv run python -m synth.pay generate --out /tmp/invoices --count 3` makes some), and watch:
the page shows the job and the invoice id at once, the approvals list grows once
Pairly's checks finish, each approval links to Pairly, and when you approve it there the
`approval.decided` webhook reaches your app (FastAPI, Flask and Express show it at
`/events`; Next.js logs it).

## What to copy into your own app

* Create the client once, server side; never send the API key to a browser.
* Verify every webhook with the **raw** body before parsing it (`construct_event` in
  Python, `constructEvent` in TypeScript), and answer 2xx quickly. Pairly retries
  failed deliveries with backoff; deduplicate on the event `id`.
* Ask for the narrowest scopes: `pay.invoice.upload` and `pay.invoice.read` to submit
  and follow invoices, `core.approval.read` to list a person's approvals,
  `core.proposal.propose` to suggest actions.

The examples are tested on every run of the test suites against a fake Pairly API
(`scripts/tests/test_quickstarts.py`, `express/server.test.ts`, `nextjs/app.test.ts`)
and type-checked with `pnpm typecheck`.
