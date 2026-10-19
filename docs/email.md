# Email: transports, Resend, inbound

Pairly sends email from two places, and both can use Resend:

| Sender | What | Code | Settings |
|---|---|---|---|
| API and worker | notifications (approvals, digests, scheduling invitations with `.ics` files, receipts for inbound invoices) | `packages/core/pairly_core/notifications/email.py` | `PAIRLY_EMAIL_TRANSPORT`, `PAIRLY_RESEND_API_KEY`, `PAIRLY_SMTP_FROM`, `PAIRLY_EMAIL_REPLY_TO` |
| Web app | Better Auth magic links, staff invitations | `apps/web/src/lib/email.ts`, `apps/web/src/lib/resend.ts` | the same variable names, read by `apps/web/src/lib/env.ts` |

Transports (`PAIRLY_EMAIL_TRANSPORT`):

- `outbox`: every message is written to `.local/mail` (`.eml`, JSON, `index.jsonl`).
  The native development stack (`make demo`, `make local-up`) uses it, so nothing ever
  needs the network. Refused unless `PAIRLY_ENV` is `development` or `test`.
- `smtp`: any SMTP relay, Mailpit in the Docker stack. Outside development and test, a
  non-local host must use TLS.
- `resend`: Resend's HTTP API. Needs `PAIRLY_RESEND_API_KEY`; without it (or with a value
  that is not a Resend key, `re_...`) the API and the worker refuse to start and the
  web server refuses its first request (it reads its settings on first use). There is
  no silent fallback to another transport.
- `fake`: sends nothing. The hosted demo uses it (see below). Refused in `production`.

Nothing enables Resend by default: `.env.example` lists the variables commented out, and
`scripts/gen_dev_env.py` never writes them.

## How Pairly calls Resend

Checked against Resend's documentation on 2026-10-19:
[send email](https://resend.com/docs/api-reference/emails/send-email),
[errors](https://resend.com/docs/api-reference/errors),
[rate limit](https://resend.com/docs/api-reference/rate-limit),
[idempotency keys](https://resend.com/docs/dashboard/emails/idempotency-keys).

- `POST https://api.resend.com/emails` with `Authorization: Bearer <key>`, JSON body:
  `from`, `to`, `subject`, `text`, `html`, `reply_to` (when `PAIRLY_EMAIL_REPLY_TO` is
  set), `headers` (`Auto-Submitted`, `X-Auto-Response-Suppress`, `List-Unsubscribe` and
  `List-Unsubscribe-Post` on non-transactional mail, `In-Reply-To` and `References` on
  replies, `Content-Language`), `tags` (`category`, which Resend shows in its dashboard;
  names and values are restricted to letters, digits, `_` and `-`), and `attachments`
  (calendar invitations: base64 content, file name and
  `text/calendar; charset=utf-8; method=REQUEST|CANCEL`).
- `Idempotency-Key`: the notification's dedupe key (hashed to SHA-256 when longer than
  Resend's 256 characters). Resend keeps keys for 24 hours, so a retry within a day never
  sends the same notification twice. The web app uses a fresh key per email, which makes
  its own retries safe.
- Errors. Retried later: network errors, 5xx, 429 (`rate_limit_exceeded`,
  `daily_quota_exceeded`, `monthly_quota_exceeded`) and a 409
  `concurrent_idempotent_requests`. Not retried: 400, 401, 403, 404, 409
  `invalid_idempotent_request` and 422. A 429 with a `Retry-After` of at most two seconds
  is retried once in place with the same key; a longer wait goes back to the delivery
  queue, which then waits at least as long as Resend asked
  (`pairly_core.notifications.delivery`). Resend's default limit is 10 requests a second
  per team.
- The From address is `PAIRLY_SMTP_FROM` for every transport (the name is historical).
  With Resend it must be on the domain you verified.

Tests with a fake HTTP layer: `packages/core/tests/notifications_tests/test_resend.py`,
`apps/web/src/lib/resend.test.ts`.

## Setting up Resend (founder checklist)

1. **Domain.** In the Resend dashboard add a sending domain. Use a subdomain you do not
   use for other mail, for example `mail.<your domain>`, so Resend's records cannot
   collide with your existing mailbox provider.
2. **DNS records.** Add exactly the records the domain's Records tab shows
   ([managing domains](https://resend.com/docs/dashboard/domains/manage-domains)):
   - SPF: a TXT record listing the senders Resend uses and an MX record that receives
     bounce and complaint feedback. Resend notes that for domains created after August
     2026 these may be CNAME records instead.
   - DKIM: a TXT record holding the public key Resend signs with.
   The host names and values are specific to your domain; copy them from the dashboard.
   - DMARC (yours, not Resend's): a TXT record on `_dmarc.<domain>`, starting with
     `v=DMARC1; p=none; rua=mailto:<a mailbox you read>;` and moving to `quarantine`
     and then `reject` once reports show every message passing
     ([DMARC guide](https://resend.com/docs/dashboard/domains/dmarc)).
   Wait until the dashboard shows the domain as verified.
3. **From address.** Choose one on that domain, for example
   `Pairly <no-reply@mail.<your domain>>`. Optionally a Reply-To mailbox someone reads.
4. **API key for sending.** Create a key with permission **Sending access** restricted to
   **that one domain** ([API keys](https://resend.com/docs/api-reference/api-keys/create-api-key):
   `sending_access` "can only send emails", and a key restricted to a domain can only
   send from it). Never a full-access key for sending.
5. **Where the key goes.** Hosted: as a Fly secret on each app that sends, never in a
   file in the repository:

   ```bash
    for a in api worker web; do fly secrets set --stage -a "pairly-demo-$a" \
      PAIRLY_EMAIL_TRANSPORT=resend PAIRLY_RESEND_API_KEY='re_...' \
      PAIRLY_SMTP_FROM='Pairly <no-reply@mail.example.com>'; done
   ```

   (start the line with a space so the shell keeps it out of its history). Locally, to try
   Resend against the real API from a shell, put the same three lines in your untracked
   `.env`; the local stack keeps using the outbox unless you do.
6. **Check.** Send yourself a magic link and confirm in Resend's dashboard that the
   message shows SPF, DKIM and DMARC as passing.

### The hosted demo and Resend

The hosted demo (`docs/hosted-demo.md`) deliberately uses `fake`: its visitors are
anonymous, they sign in through the demo page, and the public careers form accepts any
address. With Resend on, anybody could make the demo send mail to an address they type.
Turn Resend on for the demo only if that is acceptable, or for a deployment where every
recipient is a known user.

## Inbound email (Pay's intake address) through Resend

Resend can receive email (checked 2026-10-19:
[receiving](https://resend.com/docs/dashboard/receiving/introduction),
[custom domains](https://resend.com/docs/dashboard/receiving/custom-domains),
[`email.received`](https://resend.com/docs/webhooks/emails/received),
[retrieve a received email](https://resend.com/docs/api-reference/emails/retrieve-received-email),
[verifying webhooks](https://resend.com/docs/dashboard/webhooks/verify-webhooks-requests),
[retries](https://resend.com/docs/webhooks/retries-and-replays)). Its webhook carries
metadata only; the body and attachments are fetched from its Receiving API.

How it works in Pairly (`POST /v1/pay/inbound/resend`, `packages/pay/pairly_pay/api/inbound.py`
and `packages/pay/pairly_pay/resend_inbound.py`):

1. Every request first counts against a per-client-address limit (300 a minute),
   shared between API processes through Valkey like the other unauthenticated routes;
   over it, 429 (Resend retries). Then the signature is checked before anything is
   parsed: HMAC-SHA256 with the endpoint's
   `whsec_` secret over `<svix-id>.<svix-timestamp>.<raw body>`, compared in constant
   time with every `v1` signature in `svix-signature`, and the timestamp must be within
   five minutes (`pairly_core/integrations/resend/signature.py`). Anything else is 401,
   logged with the reason only, never the body.
2. Only `email.received` events are handled; others get 200 and are ignored.
3. The recipients (`to`, `received_for`) are routed with the same rules as every inbound
   email (`<workflow>+<org-slug>.<token>@<PAIRLY_INBOUND_EMAIL_DOMAIN>`). An unknown
   address, organisation or token is dropped with 200, nothing is fetched, and no bounce
   is sent.
4. The raw message is downloaded (`GET /emails/receiving/{id}`, then its
   `raw.download_url`) within the intake size limit, through the same SSRF checks as
   webhook delivery and URL ingestion (`pairly_core.webhooks.ssrf`): https only, every
   resolved address public (no private, loopback, link-local, metadata or NAT64
   address), the connection made to the checked address, each redirect checked the same
   way, and the API key never sent to the download host
   (`pairly_core/integrations/resend/receiving.py`).
5. The event id (`svix-id`, the same on every retry) is recorded in `inbound_events` in
   the organisation, in the same transaction that runs `pairly_pay.intake.receive_email`,
   the path the development drop (`make inbound-drop`) already uses. A retry of a
   processed event, or a replay inside the five minute window, is a duplicate and does
   nothing. Intake is also idempotent on the email's `Message-ID`.
6. Answers: 200 when processed, duplicate, ignored or dropped; 503 when Resend or storage
   is briefly unavailable and 500 on any other failure, so Resend retries (immediately,
   then after 5 seconds, 5 minutes, 30 minutes, 2 hours, 5 hours, 10 hours and 10 hours).

Without both settings the endpoint answers 503 and is never reachable unsigned:

- `PAIRLY_RESEND_WEBHOOK_SECRET`: the webhook's signing secret (`whsec_...`).
- `PAIRLY_RESEND_INBOUND_API_KEY`: a separate key for the Receiving API. Resend's only
  other permission level is full access (a sending-access key is refused), so this key
  can manage the whole Resend account: set it on the API app only, never on the worker
  or the web app, and rotate it if the API's secrets may have leaked.

Founder steps, only if Pay should receive real email:

1. In Resend, add the receiving domain: a subdomain used for nothing else (Resend's
   advice when the domain already has MX records), for example `in.<your domain>`, with
   the MX record the dashboard shows. Set `PAIRLY_INBOUND_EMAIL_DOMAIN` to it.
2. Create a webhook for `email.received` pointing at
   `https://<api host>/v1/pay/inbound/resend`; copy its signing secret.
3. Create the full-access key, then
   `fly secrets set -a pairly-demo-api PAIRLY_RESEND_WEBHOOK_SECRET='whsec_...' PAIRLY_RESEND_INBOUND_API_KEY='re_...'`.
4. Create or rotate the organisation's inbound address
   (`uv run pairly-admin inbound rotate --org <uuid> --workflow pay`) and forward an
   invoice to it.

The hosted demo leaves inbound off: the seeded Pay emails are written by the seed, and
`make inbound-drop` stays the development path.
