# Performance

The speed budget (BRIEF 5.3, principle 9): interactions under 200 ms (optimistic UI), page loads under 1 second on the demo data, no layout jumps. This page records the pass of October 2026 (M6 D1): what was measured, how, what was fixed, and what is left. Every number below was measured; none is estimated.

## How it was measured

* **Stack.** The native stack (no containers) seeded by `make demo`, web in `start` mode (`next build`, then `next start`), recorded model mode (no model call). Apple M4, 16 GB, everything on one machine at `nice 10`, one measurement process at a time.
* **Demo data.** 178 invoices, 34 vendors, 10 applications on 2 open jobs, 188 documents, 469 model calls, about 1,900 audit events (org `demo`).
* **Pages.** `apps/web/e2e/perf/perf.spec.ts` (Playwright, one worker, headless Chromium, 1440 x 1000). Each page loads once to warm up, then 3 times; the table gives the median. Staff sign in with a magic link from the dev outbox, the candidate and the vendor with a portal magic link.
  * **Server** is the document's server response time (`responseStart - requestStart`).
  * **Load** is navigation start to the `load` event (the streamed HTML is complete).
  * **Ready** is navigation start until no request has been in flight for 500 ms, minus those 500 ms: it includes what client components fetch after hydration, so it is the time until the page shows its data.
  * **CLS** is the sum of `layout-shift` entries without recent input during the first 3 seconds (0 is no jump; 0.1 is the usual "good" limit, the brief asks for none).
* **API.** A read-only API key of the demo admin (created for the pass, one day, never printed) and 30 sequential `GET` requests per list endpoint after one warm-up, from the same machine; p50 and p95 of the client-side time. Endpoints a key cannot call (the approvals inbox, notifications) are covered by the inbox page timings.
* **Queries.** Statement logging (`log_min_duration_statement = 0`, turned off afterwards) to count statements per request and find the slowest, then `EXPLAIN ANALYZE` on those.

Run it again on a `make demo` stack:

```bash
cd apps/web
PAIRLY_LOCAL_DIR=<the stack's folder> LOCAL_WEB_PORT=<port> PAIRLY_OUTBOX_DIR=<folder>/mail \
PAIRLY_PERF_VENDOR_ORG=<demo org id, 32 hex> PAIRLY_PERF_OUT=/tmp/perf.json \
  pnpm exec playwright test --config e2e/perf/playwright.config.ts
```

## Pages (milliseconds, median of 3)

| Page | Server before | Server after | Load before | Load after | Ready before | Ready after | CLS before | CLS after |
|---|---|---|---|---|---|---|---|---|
| Home (recruiter) | 34 | 37 | 65 | 70 | 108 | 114 | 0 | 0 |
| Hire jobs | 46 | 52 | 70 | 76 | 161 | 178 | 0 | 0 |
| Screening mode | 68 | 71 | 94 | 100 | 240 | 232 | **0.0324** | **0** |
| Pipeline board | 57 | 66 | 79 | 88 | 136 | 152 | 0 | 0 |
| Hire analytics | 55 | 58 | 79 | 82 | 124 | 128 | 0 | 0 |
| Home (hiring manager) | 36 | 37 | 69 | 65 | 112 | 101 | 0 | 0 |
| Inbox (hiring manager) | 29 | 51 | 54 | 77 | 242 | 266 | 0 | 0 |
| Home (AP clerk) | 42 | 37 | 64 | 60 | 116 | 101 | 0 | 0 |
| Invoices | 42 | 58 | 73 | 91 | 155 | 185 | 0 | 0 |
| Invoice review | 71 | 70 | 95 | 94 | 145 | 145 | 0 | 0 |
| Exceptions | 45 | 42 | 73 | 65 | 123 | 127 | 0 | 0 |
| Pay analytics | 22 | 36 | 64 | 81 | 71 | 91 | 0 | 0 |
| Home (approver) | 39 | 26 | 59 | 51 | 105 | 105 | 0 | 0 |
| Inbox (approver) | 30 | 36 | 58 | 61 | 160 | 158 | 0 | 0 |
| Home (admin) | 26 | 26 | 49 | 54 | 102 | 104 | 0 | 0 |
| Audit | 37 | 34 | 64 | 61 | 118 | 112 | 0 | 0 |
| Candidate portal | 18 | 28 | 35 | 49 | 66 | 78 | 0 | 0 |
| Candidate application | 31 | 31 | 53 | 53 | 83 | 84 | 0 | 0 |
| Vendor portal | 14 | 24 | 35 | 46 | 64 | 77 | 0 | 0 |
| Vendor portal details | 10 | 27 | 29 | 53 | 56 | 77 | 0 | 0 |

Every page is ready well under the 1 second budget (slowest: the hiring manager's inbox, 266 ms). Differences of a few tens of milliseconds between the two columns are run-to-run noise on a busy laptop, not regressions: the pages other than screening mode did not change. An earlier run, made while the worker was still reading the demo invoices, was 1.5 to 3 times slower on every page and is not used.

The one layout jump was on screening mode: the CV viewer's page area started at its minimum height (16 rem) while the PDF loaded and then grew to 60 % of the viewport, pushing the evidence list below it down (shift source `div.min-w-0.space-y-4`, the list). Fixed by reserving the page area while a PDF loads and once it shows.

## API list endpoints (milliseconds, 30 requests)

| Endpoint | p50 before | p95 before | p50 after | p95 after |
|---|---|---|---|---|
| `/v1/hire/jobs` | 4.7 | 10.3 | 4.7 | 8.7 |
| `/v1/hire/applications?limit=50` | 6.0 | 7.1 | 5.0 | 5.8 |
| `/v1/hire/pipeline/waiting` | 7.1 | 7.9 | 6.3 | 8.7 |
| `/v1/hire/requisitions` | 4.4 | 5.5 | 4.7 | 5.6 |
| `/v1/hire/scheduling/interviews` | 7.5 | 9.0 | 7.6 | 8.2 |
| `/v1/hire/analytics` | 12.3 | 16.0 | 11.6 | 12.8 |
| `/v1/pay/invoices?limit=50` | 7.8 | 8.9 | 8.0 | 8.6 |
| `/v1/pay/vendors?limit=50` | **21.9** | **27.0** | **6.5** | **7.8** |
| `/v1/pay/exceptions` | 5.5 | 6.4 | 5.8 | 8.4 |
| `/v1/pay/payment-runs` | 5.6 | 6.7 | 5.1 | 5.9 |
| `/v1/pay/purchase-orders` | 6.2 | 8.2 | 6.3 | 7.4 |
| `/v1/pay/bank-changes` | 6.2 | 7.0 | 6.0 | 7.1 |
| `/v1/pay/analytics` | 15.0 | 20.0 | 14.3 | 18.4 |
| `/v1/audit/events?limit=50` | 5.5 | 6.0 | 5.3 | 5.8 |
| `/v1/proposals` | 4.4 | 4.7 | 4.2 | 5.4 |
| `/v1/notification-templates` | 5.6 | 6.1 | 5.4 | 5.7 |
| `/v1/members` | 3.6 | 4.5 | 4.2 | 4.6 |

The vendor list was the worst offender: two queries per vendor (its active bank account and its open change ticket), 84 statements for 34 vendors, and up to 400 at the page limit of 200. It now loads both for the whole page in two queries (`pairly_pay.api.views.vendor_summaries`).

## Queries

The slowest statement logged during the pass took 3.2 ms (every `pay_invoices` row of the organisation, ordered by creation, which a Pay board reads whole). `EXPLAIN ANALYZE` on it: a sequential scan of 36 pages and an in-memory sort, 0.28 ms when cached; with 178 rows a sequential scan is the right plan, so no index was added. The audit page uses `uq_audit_events_org_id_seq` backwards (0.03 ms for 50 rows) and the vendor accounts batch uses `uq_pay_vendor_bank_accounts_one_active`. No missing index showed up on the demo data. No cache was added: nothing was slow enough to need one, and every list reads tenant data that changes with each decision.

## What is left

* **Per-request overhead.** Every API request costs about 4 ms and 16 statements before its own work (a pool pre-ping, the tenant `set_config`, the API key or user lookup, the organisation and the memberships). It is the floor of every number above. A combined identity query, or caching the membership grants for the life of a token, would cut it; not done, because the membership check on every request is what makes a removed member lose access at once.
* **Interview list.** `GET /v1/hire/scheduling/interviews` builds each interview's view with about five queries (`pairly_hire.scheduling.interview_view`). Two interviews in the demo make it cheap; batch it before an organisation has hundreds of upcoming interviews.
* **Analytics.** Hire and Pay analytics (12 and 14 ms) compute from all rows on every request. Fine at demo scale; precompute per day when the history grows.
* **Not covered by this pass.** Interaction timings (the 200 ms optimistic budget) are measured by the UX budget specs (`apps/web/e2e/tests/15-hire-ux-budgets.spec.ts`, `19-pay-ux-budgets.spec.ts`), not here; data far larger than the demo (thousands of applications or invoices) was not measured.
