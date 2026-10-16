# Security

How Pairly protects its boundaries, threat by threat. Each control names the code that enforces it and the test that proves it. To report a vulnerability, see [SECURITY.md](../SECURITY.md). The architecture these controls live in is in [architecture.md](architecture.md).

Assets: candidates' and vendors' personal data, bank details, the decisions people take, the audit trail, and the organisation's money (Pairly never moves money, but its approved payment runs and bank files are what a bank acts on).

Trust boundaries: the internet and the web app; portal people (candidates, vendor contacts); staff; agents and API keys; documents and emails (always untrusted, even from staff); model providers; webhook targets; the database and object store.

## Threat model (STRIDE)

S spoofing, T tampering, R repudiation, I information disclosure, D denial of service, E elevation of privilege.

### Prompt injection through documents (T, E)

A CV, invoice or receipt carries text meant for the model: white or tiny text, text under an image, instructions in metadata, invisible Unicode, paraphrased orders.

| Control | Code | Proof |
|---|---|---|
| Deterministic detectors on every document before any model: hidden text (colour, size, position, a renderer check for covered or clipped text), instruction phrases in English and Arabic, metadata, unusual Unicode, keyword stuffing | `pairly_core.guardrails.scan.scan_document`, `pairly_core.guardrails.detectors` | `packages/core/tests/guardrails_tests/test_detectors.py` (`test_attack_is_flagged_with_every_expected_type`, `test_a_failing_detector_fails_closed_and_others_still_run`); `evals/guardrails_hire.py` on the synthetic attack corpus |
| Flagged spans are removed and the rest is wrapped as a nonce-delimited data block; extraction only accepts input built from the document's own report | `pairly_core.guardrails.sanitize.sanitize_for_model`, `pairly_core.extraction.guarded.GuardedInput` | `test_sanitize.py` (`test_no_removed_span_or_payload_reaches_the_model`, `test_report_for_another_document_is_refused`, `test_invisible_characters_are_stripped_even_when_not_reported`) |
| Optional model classifiers can only add findings, never clear one; a failure keeps every flag | `pairly_core.guardrails.classifier.apply_classifier`, `pairly_core.guardrails.doc_classifier.classify_document` | `test_classifier.py` (`test_benign_verdict_never_clears_a_deterministic_finding`, `test_classifier_failure_keeps_every_flag`) |
| Prompts are files sent as is, with no templating: caller data can never change the instructions | `pairly_core.llm.prompt_registry` | `packages/core/tests/llm_tests` |
| Evidence quotes must exist in the text the model saw; a quote from hidden text earns nothing | `pairly_core.guardrails.validation.quote_exists`, `locate_quote` | `test_pii_validation.py` (`test_quote_from_hidden_text_gives_no_credit`) |
| A model output can never trigger a final action: a person decides, and the executor re-checks the approval in the database | `pairly_core.workflow.runtime.resume_run`, `pairly_core.approvals.executor.execute_final_action` | `apps/api/tests/test_workflow_runtime.py` (`test_forged_resume_on_the_graph_waits_again`, `test_executor_refuses_status_forged_without_decisions`) |
| End to end: hidden text in a receipt never reaches the model and a person still decides | `pairly_expense.graph` | `apps/api/tests/test_expense_acceptance.py` (`test_hidden_text_is_flagged_and_a_person_still_decides`) |

Known gap: paraphrased instructions without trigger words are caught only by the optional model classifier. The eval reports this as pending a live run ([evals.md](evals.md)).

### Data exfiltration through the model (I)

A model could be steered to put personal data into an output that leaves the organisation (a message, a summary, a trace).

| Control | Code | Proof |
|---|---|---|
| Drafting prompts see no personal data: message drafts get the template and the evidence, never the name or contact details; kits, debriefs and reference summaries get labels, not names | `pairly_hire.messaging.drafting.draft_inputs`, `pairly_hire.interviewing.kits.kit_inputs`, `pairly_hire.interviewing.debrief.debrief_inputs`, `pairly_hire.references.summary.summary_inputs`, `pairly_pay.exceptions` | `packages/hire/tests/test_pipeline_messaging_unit.py`, `test_interviewing_unit.py`, `test_references_unit.py` |
| Every draft is checked before a person sees it: no link, email address, phone number, em dash, number or name not in the inputs, protected characteristic | `pairly_hire.messaging.drafting.check_draft`, `pairly_hire.interviewing.kits.check_item`, `pairly_pay.exceptions.check_note` | `test_pipeline_messaging_unit.py` (`test_drafts_that_add_facts_are_refused`, `test_arabic_drafts_cannot_smuggle_latin_names_or_digits`) |
| Outputs are validated against a schema, ranges and banned content | `pairly_core.guardrails.validation.validate_output` | `test_pii_validation.py` (`test_validate_output_schema_ranges_and_banned_content`) |
| Nothing a model writes is sent without a person approving that exact text | `pairly_hire.messaging.graph`, `pairly_pay.messages` (approval payload hash, executor) | `apps/api/tests/test_workflow_runtime.py` (`test_executor_refuses_payload_changed_after_approval`) |
| Scoring reads blinded text: protected markers are replaced | `pairly_hire.screening.blind.blind` | `packages/hire/tests/test_screening_unit.py` (`test_blind_redaction_removes_every_protected_marker`); `evals/hire` |
| Traces and logs are redacted; spans carry ids and counts only | `pairly_core.guardrails.pii.PiiRedactor`, `redact_log_event`, `pairly_core.llm.tracing.LangfuseTracer` | `test_pii_validation.py` (`test_structlog_processor_redacts_messages_fields_and_tracebacks`), `packages/core/tests/llm_tests/test_tracing_resilience.py` (`test_langfuse_receives_only_redacted_data`), `guardrails_tests/test_redaction_counts.py` |
| The MCP server can read and propose only, through a fixed route allowlist, and drops contact details | `pairly_mcp.client` | `apps/mcp/tests/test_mcp_routes.py` (`test_no_tool_can_reach_a_decide_approve_or_pay_route`, `test_summaries_drop_contact_details`) |

### Malicious documents (D, E, T)

Zip bombs, XML external entities and billion laughs, Word macros, PDF JavaScript, decompression bombs in images, parser exploits.

| Control | Code | Proof |
|---|---|---|
| The type comes from the bytes, never the client. Macro-enabled Office files (`.docm`) sniff as ZIP and are refused by the intake policies | `pairly_core.ingestion.sniff.sniff` | `packages/core/tests/doc_tests/test_ingestion_unit.py` (`test_sniff_signatures`, `test_sniff_office_packages`) |
| Virus scan before parsing; parsing only after a clean scan | `pairly_core.ingestion.scan` | `apps/api/tests/test_expense_acceptance.py` (`test_unreadable_or_infected_receipts_go_to_a_person`) |
| Every parser runs in a separate subprocess with CPU, memory, output and file limits and a wall clock timeout | `pairly_core.documents.sandbox.run_sandboxed`, `pairly_core.documents.sandbox_child` | `test_sandbox.py` (`test_cpu_limit`, `test_wall_clock_limit`, `test_memory_limit`, `test_parse_errors_and_output_limit`) |
| DOCX: entry count and uncompressed size are capped before opening; parts with a DTD or entities are refused | `pairly_core.documents.docx` (`_check_zip`, `_safe_xml`) | `test_documents_parsers.py` (`test_docx_failures`, `test_docx_refuses_dtd_parts`) |
| XML e-invoices (UBL, CII, Factur-X) are read with defusedxml: no DTD, no entities, no external references | `pairly_core.documents.ubl`, `einvoice`, `facturx` | `test_ubl.py` (`test_never_resolves_dtds_or_entities`), `test_documents_parsers.py` (`test_xml_refuses_unsafe_or_broken`) |
| PDF JavaScript, open actions and embedded files are never run; they are reported as a warning that sends the document to review | `pairly_core.documents.pdf` | `test_documents_parsers.py` (`test_pdf_structure_warnings`, `test_pdf_limits`) |
| Images are refused above a pixel limit (decompression bombs) | `pairly_core.documents.ocr` | `test_documents_parsers.py` (`test_image_guards`) |
| Files are stored under their hash, never under the uploaded name; downloads use safe content disposition | `pairly_core.ingestion.storage` | `test_ingestion_unit.py` (`test_keys_are_built_from_org_and_hash_only`, `test_content_disposition_is_safe`) |

### BEC and vendor impersonation (S, T)

A fraudster poses as a vendor: new bank details by email, a lookalike sender domain, urgent wording, a forged invoice.

| Control | Code | Proof |
|---|---|---|
| A bank-detail change is a locked ticket: payment hold at once, a call back on the number on file (never one from the request), two approvers who are neither the requester nor the verifier, a cooling-off period, and a re-check of the locked details at activation | `pairly_pay.bank_changes` (`open_ticket`, `record_call`, `approve_bank_change`, `activate`), `pairly_pay.graph` | `apps/api/tests/test_pay_bank_changes.py` (`test_lifecycle_with_cooling_off`, `test_final_action_rechecks_dual_control`, `test_fewer_than_two_possible_approvers_fails_closed`, `test_activation_rechecks_and_fails_closed`) |
| Recording the call back needs a second factor | `pairly_core.rbac.STEP_UP_ACTIONS` (`verify`), [ADR 0012](adr/0012-verify-step-up.md) | `apps/api/tests/test_pay_bank_changes.py` (`test_lifecycle_with_cooling_off`) |
| Invoice checks: bank details that differ from the vendor's, lookalike sender domains (edit distance and confusable characters), urgency language in English and Arabic, sender domain mismatch, template changes | `pairly_pay.checks.bank.check_bank`, `pairly_pay.checks.domains.lookalike`, `pairly_pay.checks.impersonation.check_impersonation` | `packages/pay/tests/test_pay_checks.py` (`test_bank_change_attacks_are_all_caught`, `test_lookalike_domains`, `test_impersonation_attacks`, `test_urgency_language_en_and_ar`); `evals/pay` |
| A vendor's portal can only request a bank change, never make one | `pairly_pay.api.vendor_portal` | `apps/api/tests/test_pay_vendor_portal.py` (`test_contact_update_is_audited_and_bank_change_is_only_a_request`) |
| A vendor's portal never changes the phone number on file: that number is the call-back channel, so a mailbox attacker who signs in with a contact's magic link cannot choose it (only AP changes it) | `pairly_pay.portal.service.update_contacts` (`PhoneOnFileByApOnlyError`) | `test_pay_vendor_portal.py` (`test_the_portal_never_chooses_the_call_back_number`) |
| A bank mismatch never gets a drafted email: the vendor is called on the number on file | `pairly_pay.exceptions` | `packages/pay/tests/test_pay_exceptions_unit.py` (`test_a_bank_mismatch_is_never_emailed`) |

### Insider approval abuse (E, R)

A person approves their own submission, approves twice under two hats, changes the item after approval, or keeps deciding after losing the role.

| Control | Code | Proof |
|---|---|---|
| Segregation of duties: the submitter and preparers can never decide; nobody decides two steps | `pairly_core.approvals.service.decide` | `test_workflow_runtime.py` (`test_segregation_of_duties_submitter_and_preparer`, `test_all_of_chain_and_no_two_steps`, `test_delegate_who_decided_another_step_is_refused`) |
| A second factor for approve, decide, manage, export, lock, publish and verify | `pairly_core.rbac.STEP_UP_ACTIONS`, [ADR 0014](adr/0014-step-up-on-commitments.md) | `test_workflow_runtime.py` (`test_step_up_required`), `apps/api/tests/test_approvals_api.py` (`test_decision_needs_totp_and_resumes_the_run`) |
| The executor re-derives the approval from decision rows, checks the payload hash, and checks every approver still holds the permission | `pairly_core.approvals.executor.execute_final_action` | `test_workflow_runtime.py` (`test_executor_refuses_payload_changed_after_approval`, `test_executor_refuses_when_approver_lost_the_role`, `test_executor_rederives_the_approval_from_decision_rows`) |
| Signed links are single use, short lived, bound to one person, one step and one action, and need that person's TOTP sign-in | `pairly_core.signed_links` (`issue_link`, `describe_link`, `confirm_link`) | `test_approvals_api.py` (`test_link_describe_is_safe_and_confirm_decides_once`, `test_link_expiry_invalidation_and_unknown`) |
| Every decision is in the hash-chained audit log and a decision record; decision tables are append-only | `pairly_core.audit.append_audit`, `pairly_core.decisions.service.record_decision` | `packages/core/tests/test_audit_unit.py`, `apps/api/tests/test_audit_hardening.py`, `test_workflow_runtime.py` (`test_decisions_and_policies_are_append_only`) |
| Agents and API keys can only read and propose | `pairly_core.rbac.authorize` | `test_workflow_runtime.py` (`test_agent_proposes_but_cannot_approve`), `packages/core/tests/test_api_keys_unit.py` (`test_key_actor_never_gets_a_final_action`) |

### Portal account abuse (S, I, D)

A candidate or vendor contact tries to see someone else's data, reuse a link, or flood the sign-in.

| Control | Code | Proof |
|---|---|---|
| Magic links are single use, bound and expiring, and never work as a staff credential | `pairly_hire.portal`, `pairly_pay.portal` | `apps/api/tests/test_hire_portal.py` (`test_magic_link_signs_in_once_and_never_works_as_staff`), `test_pay_vendor_portal.py` (`test_magic_link_is_single_use_bound_and_never_a_staff_credential`) |
| The sign-in answers known and unknown addresses the same way, so it does not reveal who applied | `pairly_hire.api.portal` | `test_hire_portal.py` (`test_login_link_answers_known_and_unknown_addresses_in_the_same_time`) |
| Portal subjects and the portal guard (below) | `pairly_core.db.set_portal_subject`, migrations `0022`, `0023` | `apps/api/tests/test_portal_guard.py`, `test_hire_portal.py` (`test_candidates_never_reach_each_other`), `test_pay_vendor_portal.py` (`test_row_level_security_limits_a_portal_session_to_its_vendor`) |
| Rate limits per address and per email, and a bot check, on public forms and portal sign-in requests | `pairly_hire.antispam`, `pairly_hire.api.portal`, `pairly_pay.api.vendor_portal` | `apps/api/tests/test_hire_apply.py` (`test_rate_limits_per_ip_and_per_email`, `test_consent_bot_check_and_disposable_email`) |

### API key leakage (S, I)

A key ends up in a log, a repository or a client.

| Control | Code | Proof |
|---|---|---|
| Only a keyed hash of the secret is stored (HMAC with a server pepper); the secret is shown once | `pairly_core.api_keys.tokens.hash_secret`, `verify_secret` | `test_api_keys_unit.py` (`test_hash_binds_prefix_and_secret`, `test_pepper_fails_closed_without_echoing_the_value`) |
| A key can only read, propose and upload; scopes cannot exceed the creator's permissions; keys expire and can be revoked | `pairly_core.api_keys.service.check_scopes`, `authenticate_api_key` | `apps/api/tests/test_api_keys.py` (`test_scope_rules`, `test_key_permissions_are_its_scopes_only`, `test_invalid_and_expired_keys`) |
| Per-key rate limit; every use is audited | `pairly_core.api_keys.ratelimit.RateLimiter` | `test_api_keys.py` (`test_rate_limit`, `test_use_audit_and_tenant_isolation`) |
| Test keys (`pk_test_`) only work in sandbox organisations | `pairly_core.api_keys.service` | `test_api_keys.py` (`test_test_keys_need_a_sandbox`) |
| Secret scanning in CI (gitleaks); no secret has a default, and startup fails when one is missing | `.github/workflows/ci.yml`, `pairly_core.config` | `make ci-security`, `packages/core/tests/test_config.py`, `scripts/tests/test_gen_dev_env.py` |

### Webhook SSRF (I, E)

A tenant points a webhook, or a batch URL, at an internal address or the cloud metadata service.

| Control | Code | Proof |
|---|---|---|
| HTTPS only, no credentials in the URL, local host names refused; every resolved address must be public (private, loopback, link-local, metadata, CGNAT and embedded IPv4 refused) | `pairly_core.webhooks.ssrf` (`parse_target`, `resolve_target`, `is_blocked_ip`) | `packages/core/tests/test_webhooks_unit.py` (`test_blocked_addresses`, `test_unsafe_url_shapes`, `test_resolve_target_requires_every_address_public`) |
| The check runs again at delivery and the request goes to the checked address (DNS rebinding); no redirects, short timeouts, no proxy variables | `pairly_core.webhooks.delivery` | `test_webhooks_unit.py` (`test_sender_pins_the_checked_address_and_keeps_the_name`, `test_sender_refuses_unsafe_targets_and_never_follows_redirects`) |
| Batch URL fetching uses the same checks, GET only, no redirects | `pairly_core.ingestion.fetch.UrlFetcher` | `apps/api/tests/test_batches.py` (`test_urls_are_fetched_safely_and_the_batch_completes`, `test_unsafe_url_shapes_refuse_the_whole_batch`) |
| Outbound webhooks are signed (HMAC with a timestamp); inbound events are processed once by event id | `pairly_core.webhooks.signature`, `pairly_core.webhooks.inbound.record_inbound_event` | `test_webhooks_unit.py` (`test_signature_failures`), `apps/api/tests/test_webhooks.py` (`test_inbound_events_are_processed_once`) |

### Supply chain (T)

A dependency, an action or a vendored file is compromised or carries an incompatible license.

| Control | Code | Proof |
|---|---|---|
| Locked dependencies (`uv.lock`, `pnpm-lock.yaml`), installed frozen in CI | `Makefile`, `.github/workflows/ci.yml` | CI |
| Dependency audits: pip-audit with hashes, pnpm audit; static checks with Bandit | `make ci-security` | CI `security` job |
| GitHub Actions pinned to commit SHAs | `.github/workflows/ci.yml` | `make ci-actionlint` |
| License gate: every dependency must be AGPL compatible; no PyMuPDF | `scripts/check_licenses.py`, `scripts/license-exceptions.yaml` | `scripts/tests/test_check_licenses.py` |
| Vendored files are checked against a recorded checksum | `infra/api-docs/vendor/redoc` | `scripts/tests/test_build_api_docs.py` (`test_the_vendored_redoc_is_the_reviewed_mit_build`) |
| Queue messages are JSON, never pickle; checkpoints are deserialised in strict mode | `pairly_worker.main`, `pairly_core.workflow.checkpoint` | `apps/worker/tests/test_worker_settings.py` |

### Other STRIDE items

| Threat | Control | Proof |
|---|---|---|
| Spoofed staff token (S) | JWT verified against JWKS, asymmetric algorithms only, issuer, audience, expiry (`pairly_core.auth.TokenVerifier`) | `packages/core/tests/test_auth.py` |
| Cross-tenant read (I) | Row Level Security forced on every org table, plus the ORM filter ([ADR 0004](adr/0004-row-level-security-tenancy.md)) | `apps/api/tests/test_tenant_isolation.py` |
| Audit tampering or truncation (T, R) | Hash chain, database triggers, signed anchors outside the database ([ADR 0005](adr/0005-hash-chained-audit-log.md)) | `test_audit_unit.py`, `apps/api/tests/test_audit_hardening.py` |
| Model budget exhaustion (D) | Per-organisation budget with a hard stop, reserved before each request (`pairly_core.llm.budget`) | `packages/core/tests/llm_tests` |
| Stuck items that never reach a person (D) | Sweepers per workflow and the resume reconciliation job | `test_workflow_runtime.py` (`test_reconciliation_job_resumes_decided_runs`), `test_expense_acceptance.py` (`test_stuck_notes_are_restarted_once_then_sent_to_a_person`) |

## Data flows and data at rest

Personal data enters as documents (CVs, invoices, receipts), form answers, emails and portal actions. It leaves only as messages a person approved, exports a permitted person asks for, webhooks (ids and states, not documents) and model calls (sanitised, and for drafting, without personal data). The data flow of one document is drawn in [architecture.md](architecture.md#a-document-from-intake-to-decision).

| Data | Where | At rest |
|---|---|---|
| Uploaded files (CVs, invoices, receipts) | Object storage, key `orgs/<org>/blobs/sha256/<hash>` | Stored by SHA-256; signed, expiring download URLs; the database keeps the hash and size |
| Parsed document text | Object storage, beside the file | Same as the file; deleted on erasure |
| Candidate contact details (email, name, phone, location, profile link), form answers, application contact | `hire_candidates`, `hire_applications` | Encrypted (AES-256-GCM envelope, bound to organisation, table, column and row); email, name and phone also have blind indexes for search and dedupe |
| Self-identification answers | `hire_self_identification` | Encrypted; read only as counts per group for the bias report (import-linter contracts) |
| Offers' compensation, interview meeting links, reference answers and referees, survey comments, message bodies and variables, notes | Hire tables | Encrypted |
| Vendor bank details (IBAN, account name), phone on file, contacts, bank change claims and call numbers | `pay_vendors`, `pay_vendor_bank_accounts`, `pay_bank_change_tickets`, `pay_bank_change_calls` | Encrypted; IBAN and phones have blind indexes; a locked bank change is bound by a keyed digest |
| Statement counterparty IBAN | `pay_bank_statement_lines` | Encrypted with a blind index |
| Model requests and responses | `model_calls` | Encrypted; prompt and request hashes in clear for replay |
| Decision records | `decision_records` | Sensitive part encrypted; content hash in clear; linked to the audit chain |
| Notification content and recipient address | `notifications` | Encrypted; recipient has a blind index |
| Inbound email sender | `inbound_emails` | Encrypted with a blind index |
| Integration credentials, calendar refresh tokens, Zoom secrets, webhook signing secrets | `integrations`, `hire_meeting_integrations`, `webhook_endpoints` | Encrypted |
| API key secrets | `api_keys` | Keyed hash only (HMAC with a server pepper) |
| Approval links, portal sign-in links, interview, reference and survey links, upload tokens | their tables | SHA-256 of the token only |
| Audit events | `audit_events` | Clear text ids and short facts, hash chained, signed heads outside the database |
| Logs and traces | stdout, LangFuse when configured | PII redacted before writing; only the count of redactions is kept (`pii_redaction_counts`) |

Keys: data keys are wrapped by a key encryption key from the environment and can be rotated (`pairly-admin crypto rotate`, `pairly_core.crypto.rotation`). The blind index key, the API key pepper and the audit signing key also come from the environment, and each process refuses to start without them.

Retention:

| What | Rule |
|---|---|
| Rejected applications | Deletable after the organisation's retention period (`pairly_hire.retention`, set in Hire settings) |
| Pre-employment checklist files | Deletable after their retention following hire (`pairly_hire.retention`) |
| Idempotency keys, webhook outbox and deliveries, inbound event ids, old retention reports | Core rules in `pairly_core.retention.rules` |
| Job run history | Pruned per job by `job_run_start` (`pairly_core.jobs.runs`) |
| Erased candidates | Personal data removed at once on a person's request; ids, hashes and outcomes stay ([ADR 0015](adr/0015-erasure-keeps-the-trail.md)) |
| Audit events and decision records | Kept; never edited |

Retention runs as a dry run today: the `retention_dry_run` job counts what each rule would delete and never deletes. Candidate erasure is the only deletion, and only by a person.

## Tenant isolation

Every org-scoped table has Row Level Security enabled and forced, with a policy keyed on `app.org_id` (`pairly_core.migration_helpers.org_rls_statements`). The application connects as a role that owns no table and cannot bypass RLS. The ORM adds the same organisation filter on top (`pairly_core.db`). See [ADR 0004](adr/0004-row-level-security-tenancy.md).

## Portal sessions

Candidates (Pairly Hire) and vendor contacts (Pairly Pay) sign in to portals. They are not staff. A portal request binds its database session to the organisation and to a portal subject: `app.portal_subject` is `hire_candidate:<uuid>` or `pay_vendor:<uuid>` (`pairly_core.db.set_portal_subject`). Staff, agents and jobs never set it.

Two layers of restrictive policies apply while a portal subject is set. Restrictive policies are combined with AND, so each layer can only narrow what the other allows.

1. **Plugin tables.** Each plugin keys policies on its own subject (`hire_0004b` and later, `pay_0012`). A portal session sees only its own candidate's or vendor's rows, and nothing reviewers or AP wrote.
2. **Core tables (the portal guard, migrations `0022` and `0023`).** Every org-scoped core table and `users` has four restrictive policies, one per command: `<table>_portal_guard_select`, `_insert`, `_update` and `_delete`. While a portal subject is set, the table shows no rows and accepts no writes. A code path that queries a core table from a portal request by mistake sees nothing. Plugin tables with no portal policy of their own get the same guard (`portal_guard_statements`). Examples: `expense_notes`, `hire_org_settings`, `hire_messaging_settings`.

### Narrow exceptions

A rule in `portal_guard_rules` names a table, a kind of portal person (`*` for any), the commands it allows (`commands`: SELECT, INSERT, UPDATE, DELETE) and SQL conditions for rows shown and rows written. Each condition is tied to the subject. `portal_guard_apply(table)` rebuilds the table's four policies from all of its rules, each from the rules that name its command, so each plugin adds its own rules without touching anyone else's. Only migrations write rules: the application role has no privilege on the table.

No rule allows UPDATE or DELETE today: a portal person's own uploads are insert and read only. A command no rule of the table names is closed (SELECT, UPDATE and DELETE see no row; INSERT is refused).

| Table | Who | Shown (SELECT) | Written (INSERT) |
|---|---|---|---|
| `documents` | any portal person | files they uploaded (`portal_subject`, filled by the database from the setting) | only as themselves |
| `templates` | any portal person | yes (the email texts) | never |
| `webhook_outbox` | any portal person | never | yes (an upload emits `document.received`) |
| `audit_events` | candidate | never | events with the candidate as actor |
| `audit_events` | vendor | never | events with one of the vendor's portal contacts as actor |
| `decision_records` | candidate | decisions about their own applications and CVs, and steps they took | only as themselves |
| `decision_records` | vendor | steps one of the vendor's contacts took | only as such a contact |
| `appeals` | candidate | their own (`hire_candidate:<id>`) | only their own |
| `documents` | candidate | the CVs of their applications, the files of their document requests | (see above) |
| `policy_versions` | candidate | the rubric versions of decisions they can see | never |
| `hire_jobs`, `hire_pipeline_configs` | candidate | the jobs, and their stages, of their own applications | never |

Every other org-scoped table shows a portal session nothing and refuses its writes. That includes notifications, approvals, workflow runs, model calls, search chunks, extractions, memberships, users and API keys.

### System steps of a portal request

Some engine work caused by a portal action is not the portal person's to see. `pairly_core.db.outside_portal(session)` clears the portal subject for one block of such work in the same transaction and then restores it. The organisation boundary still applies. Pending writes are flushed first, so they are checked against the portal policies. It is used only for:

* queueing a notification (`pairly_core.notifications.service.notify`): recipient preferences, addresses and dedupe rows;
* finding Hire's admins for a notice about a data request (`pairly_hire.portal.notices`);
* finding the head of a decision chain (`record_decision`): the id only.

The audit chain head (sequence and hash only) comes from `audit_chain_head()`, and the chain link trigger reads it the same way. A portal person's event therefore continues the chain, but the portal never reads the log.

A portal upload of bytes that someone else's file already has gets a plain 409 ("already received"), which names nothing.

### Tests

`apps/api/tests/test_portal_guard.py` lists every org-scoped table in the migrated database and checks:

* each table has a restrictive portal policy;
* after real vendor and candidate portal flows, a portal session of anyone else sees no row of any table except the shared templates and Pay's settings;
* the portal person sees, among core tables, only what the rules above allow;
* writes outside the rules are refused;
* every rule names its commands and none allows UPDATE or DELETE; a forged UPDATE or DELETE on every exception table, by the vendor and by the candidate, touches no row.
