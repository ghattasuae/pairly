# pairly-pay

Pairly Pay workflow plugin (`pairly_pay`): accounts payable.

Built so far (M4):

* **Vendors** (`vendors.py`, `api/vendors.py`): the vendor master with encrypted
  contacts, phone number on file and bank accounts (IBAN blind index for lookups),
  dedupe on TRN, IBAN and similar names (`dedupe.py`), and a risk profile.
* **Validation**: IBAN ISO 13616 with the country length table and bank code
  extraction (`iban.py`); UAE TRN format with a `TrnVerifier` hook (`trn.py`). No
  official TRN source is connected yet, so nothing reads as verified.
* **Onboarding** (`onboarding.py`, `api/onboarding.py`): a signed single-use invite
  link opens a short session for the vendor's own form; submissions wait for an AP
  review.
* **Bank-detail changes** (`bank_changes.py`, `holds.py`, `graph.py`): a locked change
  ticket holds payments at once; a call back on the number on file only; dual approval
  by two people who are neither the requester nor the verifier; optional cooling-off;
  activation by the `pay_bank_change_activation` job. Every step audited and recorded
  as a decision record.

* **Invoice intake** (`intake.py`, `email_rules.py`, `api/invoices.py`): the inbound
  address `pay+<org-slug>.<token>@<domain>` (made with `pairly-admin inbound rotate`),
  upload and API (keys with `pay.invoice.upload`), all through the engine's ingestion
  with file hash dedupe. Several attachments become several invoices, terms and logos
  stay with the email, an invoice in the body is read from the body, forwarded chains
  keep the original sender's domain, marketing is ignored, unclear mail goes to a
  person, and the sender gets a receipt with a reference (engine backscatter rules).
* **Extraction** (`invoice_schema.py`, `invoice_graph.py`, `einvoice.py`): the
  `pay_invoice` graph scans, parses, runs the guardrails, then reads the invoice with
  the model (`Extracted[T]` fields with quotes and spans, Decimal money, per-field
  thresholds) or, for UBL 2.1 / PINT AE XML, exactly without a model; it links the
  vendor (`matching.py`: TRN, IBAN blind index, sender domain, name) and stops at
  `ready` or `needs_review`. `CHECKS` in `invoice_graph.py` is where the validation and
  fraud checks of the next step plug in. Approvals and payments start from `ready`.
* **Learning** (`learning.py`): corrections are filed per vendor and the vendor's
  recent ones go into the next extraction prompt as hints (never bank details).
* **Recordings** (`data/recordings.json`, `recordings.py`): scripted responses (from the
  synthetic ground truth, not a model) for an eval subset and the demo trio, so intake
  runs offline. Regenerate with `uv run python -m synth.pay recordings`.

Routes are mounted under `/v1/pay`; the Alembic branch is `pay` (`migrations/`).
