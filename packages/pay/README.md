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

Routes are mounted under `/v1/pay`; the Alembic branch is `pay` (`migrations/`).
