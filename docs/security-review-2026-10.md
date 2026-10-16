# Security review, October 2026 (M6 D1)

A review of every threat in [security.md](security.md) (BRIEF 6 and 11): for each STRIDE threat the named control and its test were read, then attacked with a concrete attempt in a test, run against the test database only (never an external system, never a real model call). The three changes made just before the review (response reuse, blind-placeholder credit, label-plus-value table evidence) and the Row Level Security and portal guard were read again from scratch.

Every high finding is fixed in code with a test. Medium and low findings are fixed when the fix was small and safe; the rest are listed with a proposed fix.

## Findings

| Id | Severity | Threat | Finding | Status |
|---|---|---|---|---|
| H-01 | High | BEC and vendor impersonation | The vendor portal could change the phone number on file, which is the call-back channel of every bank-detail change | Fixed, `d89a040` |
| H-02 | High | Malicious documents (D), batch URL intake | The batch URL fetcher decompressed response bodies (gzip, deflate, zstd) before the size limit: a decompression bomb against the shared API process | Fixed, `daca502` |
| M-01 | Medium | Insider approval abuse | A delegation kept its seat after the delegator lost the role or left; the executor checked only the delegate | Fixed, `23d7670` |
| M-02 | Medium | API key leakage, elevation | An API key kept every scope after its creator was moved to another role or removed | Fixed, `9690234` |
| M-03 | Medium | BEC, insider | Bank details a staff member enters on a new vendor are active at once, with no second person | Open, proposed fix below |
| M-04 | Medium | BEC, insider | The person who changed a vendor's phone number on file (staff) may then record the call back on that number | Open, proposed fix below |
| M-05 | Medium | BEC | Vendor onboarding: the call-back number comes from the same form as the bank details | Open, proposed fix below |
| L-01 | Low | Recent change: response reuse | Reuse could pick a live decision replay (`replay_of`) once the original left the window | Fixed, `0e09c1f` |
| L-02 | Low | Supply chain | CI ran `uv sync` without `--locked` | Fixed, `1b9ec47` |
| L-03 | Low | API keys, portals, public forms (D) | Rate limits live in each API process's memory | Open (known TODO) |
| L-04 | Low | Malicious documents | A package declaring the plain DOCX type but carrying a VBA project or ActiveX parts was accepted | Fixed, `e7c36fc` |
| L-05 | Low | Recent change: blind-placeholder credit | A visible part of two letters or digits ("of", "20") counts as evidence | Fixed |
| L-06 | Low | Recorded mode | `find_recording` serves the newest row of a hash, which can be a live decision replay | Fixed |
| L-07 | Low | RLS, SECURITY DEFINER | `ops_tenant_usage` and `job_runs_recent` return more than ids and counts (slugs, job summaries) and the app role may call them; only the Python route limits them to platform operators | Accepted risk |
| L-08 | Low | Repudiation | `job_run_start` and `job_run_finish` (SECURITY DEFINER) accept any job name or run id from the app role | Accepted risk |
| L-09 | Low | Information disclosure | A public application whose CV has the same bytes as another candidate's is linked to that candidate's document row | Open, proposed fix below |
| L-10 | Low | BEC | Vendor portal contacts (people and emails) change directly; AP messages go to those addresses | Open, proposed fix below |
| L-11 | Low | Supply chain | The vendored Redoc checksum is checked by a test, not by `make api-docs` | Fixed |
| L-12 | Low | Malicious documents | A DOCX with an external attached template (remote template injection) is accepted | Fixed |

No critical finding. Nothing found in: prompt injection reaching a model input or an action, data exfiltration through the model, approval link replay, approval races, portal token forgery and cross-portal reads, webhook signatures and SSRF, RLS coverage, and the label-plus-value table evidence.

## High

### H-01 The vendor portal chose the call-back number (fixed)

**Repro.** Someone holds a vendor contact's mailbox, the usual business email compromise foothold. They ask the vendor portal for a magic link, sign in, and `PUT /v1/pay/vendor-portal/details/contacts` with `{"phone_on_file": "<their number>"}`. The change was applied and only flagged bank-detail changes opened within the next 30 days (`phone_recently_changed`). They wait 31 days and `POST /vendor-portal/bank-change`. The ticket snapshots the number on file, which is now theirs: the AP clerk calls the attacker, records "confirmed", and the two approvers see a confirmed call. Every control of the bank-detail workflow then passes, and the new account is paid.

**Fix.** The portal no longer changes the phone number on file (`pairly_pay.portal.service.update_contacts`, `PhoneOnFileByApOnlyError`, 403 `phone_on_file_by_ap_only`, nothing written); the current number again is a no-op. Only accounts payable changes it, and the portal page tells the vendor to call AP. People at the vendor still change directly (BRIEF 4.7.3).

**Test.** `apps/api/tests/test_pay_vendor_portal.py::test_the_portal_never_chooses_the_call_back_number`; the web component test checks the field is gone.

### H-02 Decompression bomb through a batch URL (fixed)

**Repro.** A tenant (any API key or person with a workflow's upload permission) creates a batch with a URL to a server it controls, which answers `200` with `Content-Encoding: zstd` and a body of a few kilobytes that decodes to gigabytes. httpx decodes a whole network chunk in one call (zstandard is installed), so the API process allocates the decoded bytes before the size limit sees one; the shared API process runs out of memory for every tenant. gzip and deflate behave the same within their ratio. Webhook delivery already read raw bytes; the fetcher did not.

**Fix.** `pairly_core.ingestion.fetch.UrlFetcher` sends `Accept-Encoding: identity`, refuses any other `Content-Encoding` before reading the body (`content_encoding_refused`) and counts and stores raw bytes (`aiter_raw`).

**Test.** `packages/core/tests/doc_tests/test_fetch_unit.py` (every encoding refused before a byte of the body is read; the limit counts raw bytes).

## Medium

### M-01 A delegation outlived its holder's role (fixed)

**Repro.** `appr1` (an approver) delegates to `fc1`, who holds the approve permission but is not an approver of the step. `appr1` leaves the organisation. `fc1` still decided on `appr1`'s seat, and the executor accepted it because it checked the permission of the person who decided, not of the approver they stood in for. In Pay, a departing finance controller's delegation kept a plain approver able to complete finance steps.

**Fix.** `decide` lends a delegated seat only while the delegator is still a member; the executor requires every principal of a delegated decision to still hold the permission (`approver_lost_permission`).

**Tests.** `apps/api/tests/test_security_review.py` (`test_a_delegation_dies_with_the_delegators_role`, `test_the_executor_refuses_a_seat_whose_holder_lost_the_role`), with the attempts that found nothing: `test_the_submitter_cannot_approve_as_a_delegate`, `test_keys_and_agents_never_decide`, `test_two_approvers_racing_record_one_decision`.

### M-02 An API key outlived its creator's role (fixed)

**Repro.** An admin creates a key with read and upload scopes, is moved to auditor, then removed. The key kept every scope for up to a year (scopes were checked against the creator only at creation), including reading candidates' and vendors' data.

**Fix.** `authenticate_api_key` intersects the key's scopes with what its creator holds today; a key with nothing left is refused (403 `api_key_creator_inactive`).

**Test.** `apps/api/tests/test_api_keys.py::test_a_key_never_outlives_its_creators_role`.

### M-03 Staff vendor creation activates bank details at once (open)

`POST /v1/pay/vendors` with bank details makes them the active account (documented: "the vendor is new, nothing was paid to it yet"). One insider can create a vendor that copies a real one under a slightly different name with their own IBAN; duplicate checks refuse the same TRN or IBAN and warn on similar names, but `acknowledge_similar` passes. Payment still needs an approved invoice and a finance controller for the run, so it takes collusion or a careless approval.

**Proposed fix.** Create staff-entered bank details as `proposed` and activate them through the same ticket (call back plus dual approval), or at least require a second person with `pay.vendor.write` to confirm a new vendor before its first invoice can be approved.

### M-04 The phone changer can verify on the new number (open)

A clerk may `PATCH` a vendor's phone number on file (no ticket open) and then record the call back of a ticket the vendor or the system opened, on that number. The flag `phone_recently_changed` shows it, and two approvers who are neither requester nor verifier are still needed.

**Proposed fix.** Store who last changed the number (`phone_changed_by`) and refuse `record_call` by that person while the change is within the window; better, make a staff phone change itself need a second person.

### M-05 Onboarding call-back number comes from the onboarding form (open)

A vendor invited to onboard enters its bank details and its phone number on the same form. Accepting the onboarding opens a ticket whose call back goes to that number, so an attacker who intercepted the invitation verifies their own details.

**Proposed fix.** When accepting an onboarding with bank details, require the clerk to enter the call-back number from an independent source (the trade licence registry, an earlier contract), compare it to the form, and snapshot the entered one on the ticket.

## Low

* **L-01 (fixed).** `DatabaseCallStore.find_reusable` now also requires `replay_of IS NULL`. Test: `apps/api/tests/test_llm_router.py::test_reuse_never_crosses_orgs_or_reuses_a_replay`, which also checks on the real table that an identical request of another organisation reaches the model and never reuses.
* **L-02 (fixed).** Every `uv sync` in `.github/workflows/ci.yml` uses `--locked`.
* **L-03.** API key, portal and public form limits are per process (`pairly_core.api_keys.ratelimit`, `TODO(M6)` there): with N API processes the effective limit is N times the setting. Proposed fix: the Valkey token bucket the TODO describes, before the API runs replicated.
* **L-04 (fixed).** `pairly_core.ingestion.sniff` treats a package with `vbaProject.bin`, `vbaData.xml` or `word/activeX/` parts as ZIP, so it is refused like a `.docm`. Test: `test_ingestion_unit.py::test_a_docx_that_carries_macros_or_activex_is_not_a_docx`.
* **L-05 (fixed).** `EvidenceLocator.locate_visible` kept a visible part with two letters or digits ("of", or "20" cut from "2019"). Placeholder text itself was never evidence, so no protected marker leaked; the risk was a score above 0 with weak evidence. A visible part now counts only when it holds one real word (`_real_word`: a run of at least three letters or digits, any script) or is a complete number (`12`, `3.7`, `1,200`, `15%`, with no letter or digit right before or after it); anything shorter is dropped. Tests: `packages/core/tests/extraction_tests/test_evidence_visible.py` and `packages/hire/tests/test_screening_unit.py::test_a_quote_over_a_blind_placeholder_is_credited_with_its_visible_parts_only` (two letters cut from a word and the start of a year earn nothing).
* **L-06 (fixed).** Recorded mode replayed the newest row of a request hash with an output, which included a live decision replay, so a live replay changed what recorded runs returned. `DatabaseCallStore.find_recording` now requires `replay_of IS NULL`, as `find_reusable` and `pairly_core.llm.bundle` already did (the in-memory test stores do the same). Test: `apps/api/tests/test_llm_router.py::test_recorded_mode_never_serves_a_decision_replay`, on the real table (a hash whose only row is a replay has no recording).
* **L-07 (accepted).** The two platform functions return organisation slugs and per-tenant counts, and job summaries, to any caller with the app role; `/v1/ops/*` returns 404 to everyone but people of the system organisation. Reaching them needs arbitrary SQL as the app role, and the data holds no tenant content. Revisit if the platform board gets more columns.
* **L-08 (accepted).** Job run history can be started or finished by any code path with the app role. It is operational history, not the audit log (which is hash chained and anchored).
* **L-09.** Dedupe at intake (`uq_documents_org_id_workflow_sha256`) links a second applicant with byte-identical CV bytes to the first applicant's document row, so the second candidate's portal shows the first one's original file name, and erasing either affects both. Proposed fix: dedupe the stored blob only and give each submission its own `documents` row.
* **L-10.** A mailbox attacker in the portal can add a contact email; AP messages are sent to an address a person picks and approves, never automatically. Proposed fix: mark contacts added through the portal as new for 30 days in the recipient picker.
* **L-11 (fixed).** `scripts/build_api_docs.py` embeds the vendored Redoc bundle without checking its SHA-256; only `scripts/tests/test_build_api_docs.py` did. The build now reads the bundle through `read_bundle`, which compares its SHA-256 with the recorded `REDOC_SHA256` before anything is written and stops `make api-docs` (non-zero exit, `BundleChecksumError`) on any other bundle. Test: `scripts/tests/test_build_api_docs.py::test_a_changed_bundle_stops_the_build_before_anything_is_written`.
* **L-12 (fixed).** A DOCX whose `word/_rels/settings.xml.rels` names an external `attachedTemplate` was accepted. Pairly never opens it in Word, but a person downloading it might. `pairly_core.ingestion.sniff.active_content` now reads every relationships part (size capped, defusedxml, an unreadable one fails closed) and a package with any `TargetMode="External"` relationship sniffs as ZIP, like L-04: a remote attached template, an external image, frame, OLE object or subdocument. The one exception is a hyperlink, which Word never follows by itself (CVs carry profile links). Intake refuses it with its own reason (`RejectReason.ACTIVE_CONTENT`, 415 with a title that names it; the audit event's `detail` says `macros`, `external_template`, `external_relationship` or `unreadable_relationships`). Tests: `packages/core/tests/doc_tests/test_ingestion_unit.py` (`test_a_docx_with_a_remote_template_or_external_part_is_not_a_docx`, `test_hyperlinks_and_internal_relationships_leave_a_docx`) and `apps/api/tests/test_documents.py::test_a_docx_with_a_remote_template_is_refused_with_its_reason`.

## Threat by threat

### Prompt injection through documents

Read `scan_document`, `sanitize_for_model`, `GuardedInput`, `quote_exists`, `resume_run` and `execute_final_action`. Every intake path (upload, API, batch URL, email, both portals, the public application form, policy documents, expense receipts, onboarding) stores through `ingest_spooled`, parses only after a clean scan in the sandbox, and builds model input from the guardrail report of that document. No model output triggers a final action: rule approval of an invoice (`rule_refusals`) needs low risk and `requires_human` false, which an injection finding never gives, and a payment run always needs a person. Existing attempts: `test_detectors.py`, `test_sanitize.py`, `test_expense_acceptance.py::test_hidden_text_is_flagged_and_a_person_still_decides`. No finding.

### Data exfiltration through the model

Model input is built from the request's organisation only: retrieval, few-shot examples and coding history run under Row Level Security, prompts are files sent without templating, and no secret is ever in an input. Response reuse filters on `org_id` and RLS; the new database test shows another organisation's byte-identical request reaches the model (L-01). No finding.

### Malicious documents

H-02, L-04 and L-12 fixed. Read the sniffer, the sandbox limits (CPU, memory, output, wall clock), the DOCX entry and size caps and DTD refusal, defusedxml for every XML format, PDF JavaScript and embedded files reported, the image pixel cap, and the render page cap and time budget. Every intake path uses the same sniffing and policy.

### BEC and vendor impersonation

H-01 fixed; M-03, M-04, M-05 and L-10 open. Paths that could change bank details: staff creation (M-03), onboarding acceptance (ticket, M-05), the portal (request only, H-01), an invoice (ticket and hold), an extraction correction (payment always goes to the vendor's active account, `approved_account_id` and `approved_bank_digest`, re-checked at the payment run), the API (keys cannot hold `write`). There is no vendor CSV import.

### Insider approval abuse

M-01 fixed. Self-approval through delegation, a key or an agent deciding, and racing approvers are refused (`test_security_review.py`); signed links are single use and bound to a person, a step, an action and a TOTP session (`test_approvals_api.py`).

### Portal account abuse

H-01 fixed. Tokens bind organisation, row and subject into a SHA-256 digest compared in constant time, and the session is bound to the claimed subject before any lookup. Cross-candidate and cross-vendor reads through applications, messages, uploads, receipts, appeals and export are covered by `test_hire_portal.py` and `test_pay_vendor_portal.py`; the notification stream is staff only and filtered per recipient. No other finding.

### API key leakage

M-02 fixed, L-03 open. Keys are stored as a keyed hash, test keys work only in sandboxes and live keys never there, the organisation header must match the key's, and a key never holds approve, decide, manage or export.

### Webhook SSRF and signatures

Numeric spellings of internal addresses are refused and the request goes to the checked address (`packages/core/tests/test_security_review_unit.py`); redirects are never followed; signatures are HMAC with a timestamp tolerance, compared in constant time. No finding.

### Supply chain

L-02 fixed, L-11 open. The license gate, `pnpm install --frozen-lockfile`, pinned actions and the Redoc checksum test are in place.

### Recent changes

* **Response reuse (`c69be99`).** Never crosses organisations (filter plus RLS, new database test). Erased rows are excluded and erasure also erases every row with the same request hash. Replays could be reused (L-01, fixed). Migration `0029` runs against a fresh database and `alembic check` passes.
* **Blind-placeholder partial credit (`54a18b6`).** Only visible ranges are mapped back and each is checked against the original; placeholder text is never evidence. L-05 (a quality point) is fixed.
* **Label-plus-value table evidence (`c1d4953`).** A line is used only when unique in the model's text, it holds the value, and the page layout puts it beside or under another line of the quote; it is located in the sanitised text, so hidden text never qualifies. Payment never uses the invoice's IBAN. No finding.

### Row Level Security and the portal guard

On the migrated database: every table with `org_id` has Row Level Security enabled and forced (the only tables without it are `alembic_version` and `portal_guard_rules`, which have no `org_id` and no app privilege). The 41 permissive policies keyed on scan settings (`app.ops_scan` and the like) apply to the owner role only, so the app role setting those variables gains nothing. SECURITY DEFINER functions: all set `search_path`; the ones returning more than ids or counts are `my_organisations` (the caller's own organisations), `hire_careers_org` (a public careers page's name), and the two platform functions (L-07). `erase_extraction_values` writes only in the caller's organisation. The portal guard test covers every org-scoped table.
