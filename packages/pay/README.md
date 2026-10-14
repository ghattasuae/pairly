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
  In development there is no mail receiver: `make inbound-drop FILE=<eml>` delivers a
  file to the org whose address is in its `To` header, and the worker's
  `pay_inbound_drop` job delivers files dropped in `.local/inbound/`
  (`inbound_drop.py`; refused outside development and test).
* **Extraction** (`invoice_schema.py`, `invoice_graph.py`, `einvoice.py`): the
  `pay_invoice` graph scans, parses, runs the guardrails, then reads the invoice with
  the model (`Extracted[T]` fields with quotes and spans, Decimal money, per-field
  thresholds) or, for UBL 2.1 / PINT AE XML, exactly without a model; it links the
  vendor (`matching.py`: TRN, IBAN blind index, sender domain, name) and stops at
  `ready` or `needs_review`. Approvals and payments start from `ready`.
* **Validation and fraud checks** (`checks/`, run from `CHECKS` in `invoice_graph.py`):
  arithmetic and VAT per tax code (5% default, zero-rated, reverse charge; Decimal, one
  minor unit tolerance), duplicates and near duplicates naming the suspected original,
  bank details against the vendor master (a mismatch opens a change ticket by itself, so
  the vendor's payments are held), sender domain and lookalike domains, first-time
  senders and urgency language (English and Arabic), unusual, round and split amounts,
  template, metadata and editing-trace signals, the guardrail findings, the `pay.invoice`
  rule set and the tenant's spend policy (engine policy search; "no policy found" is
  information, not a pass), and the risk score (engine combiner, `policy/
  invoice_risk_policy.yaml`). Each check returns pass, fail or error; an error sends the
  invoice to a person. Every finding has one sentence in English and Arabic. Settings per
  organisation in `pay_settings.check_settings` (`checks/settings.py`). Measured by
  `uv run python -m evals.pay`.
* **Checks stay current** (`freshness.py`, `checks/service.py` `recheck_invoice`): the
  checks store a digest of what they read (values with corrections, vendor, the
  vendor's bank account on file, coding). A correction, a vendor link or a coding change
  runs every check and the risk again in the same transaction, so a bank-detail
  correction that differs from the master opens or joins the change ticket and holds the
  vendor. An invoice whose checks are stale or failed can never be approved or paid.
* **Approvals** (`approvals.py`, `approval_graph.py`, `policy/invoice_approval_rules.yaml`):
  a person sends a `ready` invoice for approval; the engine picks the chain from the
  versioned rules (amount, department, category, vendor risk, risk level), with SLA,
  escalation, delegation, out-of-office, the inbox, signed email links and Slack (which
  opens the web confirmation page). Everyone who processed or corrected the invoice is a
  preparer and never approves; nobody approves two steps. The `approve_invoice` final
  action checks holds, open bank-detail changes, check freshness and the bank details
  again. Approval by a rule is off by default (`payment_settings.py`) and never applies
  to medium, high or critical risk; it is recorded as a decision record of its own.
* **Payment runs** (`payment_runs.py`, `payment_run_graph.py`, `bank_files.py`): propose
  from approved invoices (one currency, due by a date, oldest first, up to a cash limit;
  what is left out says why), a finance controller approves (never the proposer), then
  the bank file: CSV or ISO 20022 pain.001.001.09 built with the standard library. At
  export every item is checked again against the vendor master and the digests it was
  approved with; any change refuses the file (409, audited). A person marks invoices
  paid. No bank connection, no money moves. Early-payment discounts are a placeholder.
* **Learning** (`learning.py`): corrections are filed per vendor and the vendor's
  recent ones go into the next extraction prompt as hints (never bank details).
* **Recordings** (`data/recordings.json`, `recordings.py`): scripted responses (from the
  synthetic ground truth, not a model) for an eval subset and the demo trio, so intake
  runs offline. Regenerate with `uv run python -m synth.pay recordings`.

Routes are mounted under `/v1/pay`; the Alembic branch is `pay` (`migrations/`).
