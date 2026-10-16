# ADR 0012: A second factor for recording an out-of-band verification

Status: accepted

## Context

Pairly Pay's bank-detail change workflow (brief 4.1.4, WP4.3) asks a staff member to
call the vendor on the phone number already on file and record what the vendor said.
That record is the evidence the two approvers then rely on: an approval of a change
nobody really confirmed is worthless. Recording it is a human commitment of the same
weight as an approval, but naming the permission `approve` would be misleading in the
audit log and the permission list, and it would let the approval engine treat the
verifier as an approver.

## Decision

`verify` joins the step-up actions (`pairly_core.rbac.STEP_UP_ACTIONS`), next to
`approve`, `decide`, `manage`, `export`, `lock` and `publish` (ADR 0010). The rule stays
keyed on the action, so custom roles and later workflows inherit it. No existing
permission used the action, so nothing that worked before changes. The agent role and
API keys can never hold it (it is neither `read`, `propose` nor `upload`).

## Consequences

* `pay.bank_change.verify` needs a sign-in with TOTP. `GET /v1/me` lists it under
  `second_factor_permissions`, so the web app asks for the second factor first.
* A workflow that adds another kind of verification gets the second factor for free by
  naming the permission `<workflow>.<resource>.verify`.
