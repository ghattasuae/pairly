# Build brief: Pairly AI, an open-source platform for safe AI agents in back-office work

You are the lead engineer building Pairly. Read this entire brief before writing any code. It is long on purpose: nothing here is optional unless marked OPTIONAL or LATER. Where something is marked ASK, stop and ask me before deciding. Where a fact is missing, leave a clearly marked `TODO:` and list it in your milestone summary.

Pairly is my flagship open-source project. It has three jobs:

1. **Solve real, painful problems** in two areas every company has: hiring and paying invoices.
2. **Prove production-grade AI engineering in public**: agents, RAG, evaluation, security, human oversight, observability, and a clean architecture anyone can read.
3. **Be demoable live in 5 minutes** to a CEO, a recruiter, or an engineer, and runnable by anyone with one command.

Name: platform **Pairly AI** (short form Pairly), workflows **Pairly Hire** and **Pairly Pay**. The name means the AI and the human work as a pair: the AI reads, checks and prepares, the human decides. Tagline: "AI does the work. You make the call. Together." GitHub organisation: `pairly` if free, otherwise `pairly-ai` or `pairlyhq` (ASK).

---

## 0. Hard rules

1. **Humans decide what matters.** No agent ever rejects a candidate, sends an offer, approves a payment, changes vendor bank details, or deletes data on its own. Agents propose, humans approve. This is enforced in code (LangGraph interrupts plus server-side permission checks), never only in prompts.
2. **Synthetic data only.** Never commit real CVs, real invoices, real names, real bank details, or any real personal data. All fixtures and demo data are generated.
3. **No real money moves.** Payment features produce payment proposals and bank export files and mark records as paid. No live bank integrations.
4. **Fail closed.** If a check cannot run (model down, parser error, missing data), the item goes to a human queue. It is never auto-approved.
5. **Every AI decision is explainable and logged**: inputs, model, prompt version, output, evidence, confidence, cost, and who approved what.
6. **Evaluations gate merges.** CI runs the eval suites; a drop below thresholds fails the build.
7. **Security by default**: least privilege, encrypted sensitive fields, signed links, rate limits, idempotent webhooks, audit trail.
8. **Copy style** for all UI text, emails, docs and README: plain, short sentences, no hype words, **never use em dashes** (use commas, colons or full stops).
9. **Do not invent metrics.** Any number in the README or docs must come from an actual eval run.
10. **Ask before** creating external accounts, buying anything, or deploying publicly.
11. **Licensing is decided**: dual license, AGPL-3.0 for everyone plus a paid commercial license from Proofly Technology (see section 9.1). Every source file carries the license header; every dependency must be AGPL-compatible.

---

## 1. The pain we are solving (research summary, use it in docs and demo)

### Hiring
- Interview scheduling alone eats roughly 35 to 38% of a recruiter's time.
- AI-generated applications flood roles: around 254 applications per role in one 2026 analysis, and 67% of HR leaders say reviewing AI-generated applications slowed their process.
- Candidates are ghosted: one 2026 index of 200,000+ recruiter-candidate exchanges found 72% of candidates in active pipelines go 30+ days without follow-up, median silence 75 days.
- Candidate fraud is now a volume problem: 6% of job seekers admit interview fraud (Gartner), an analysis of 19,000+ interviews flagged 38.5% for AI-cheating behaviour, and recruiters report prompt injections hidden inside CVs and deepfakes in video interviews.
- Trust is broken: only 26% of candidates trust AI to evaluate them fairly.
- Regulation is arriving: NYC Local Law 144 (bias audits, candidate notice), Illinois and Colorado rules (notice, human review of adverse decisions), and the EU AI Act classifying recruitment AI as high-risk (human oversight, logging, documentation; high-risk obligations currently scheduled for December 2027).

### Accounts payable
- 76% of organisations experienced attempted or actual payments fraud in 2025 (AFP 2026), with business email compromise and vendor impersonation the top vectors.
- The most damaging pattern is the **vendor bank-detail change**: the vendor and invoice are real, only the bank account changes, so ordinary controls miss it. Best practice is out-of-band verification and dual control.
- Duplicate payments are estimated at 0.5 to 1.2% of total spend, and near-duplicates (slightly altered numbers, dates, amounts) slip past humans.
- Manual AP averages many touches per invoice and days of cycle time; approvals still happen over email; exceptions pile up after "automation" go-lives.
- AI-generated fake invoices and receipts are rising.
- UAE context: B2B e-invoicing is mandated from January 2027 (PINT AE / Peppol-based), VAT is 5%, and supplier TRNs must be validated.

**Positioning line:** "Pairly lets AI do the reading, checking and paperwork, while humans keep every decision that matters, with proof of why."

---

## 2. Architecture

### 2.1 Overview
A monorepo with one **core engine** and two **workflow packages** on top. The engine knows nothing about hiring or invoices; workflows plug in through clear interfaces. Adding a third workflow (for example insurance claims, LATER) must require no engine changes. Prove this in the docs with a "How to add a workflow" guide.

```
pairly/
  apps/
    api/            FastAPI app (REST + webhooks + auth integration)
    worker/         background jobs (queues, scheduled tasks)
    web/            Next.js app: admin dashboard, approval inbox, candidate portal, vendor portal
    mcp/            MCP server exposing safe read and propose tools
  packages/
    core/           engine: ingestion, documents, extraction, policy, risk, approvals, audit, notifications, scheduling, guardrails, model router, evals hooks
    hire/           Pairly Hire workflow
    pay/            Pairly Pay workflow
    shared-ts/      TypeScript types generated from the API schema
  evals/            datasets, PromptFoo configs, eval runners, reports
  synth/            synthetic data generators (CVs, jobs, invoices, vendors, POs, attacks)
  infra/            docker compose, migrations, seed scripts
  docs/             architecture, ADRs, security, compliance, workflow guides
```

### 2.2 Core engine modules
1. **Tenancy and RBAC**: organisations, users, roles (admin, recruiter, hiring manager, interviewer, AP clerk, approver, finance controller, auditor, read-only), per-workflow permissions, row-level tenant isolation.
2. **Ingestion**: upload, inbound email (dedicated addresses per tenant and workflow), public forms, API. Every incoming file is hashed (SHA-256) for dedupe, virus-scanned, size and type checked, and stored in object storage with a signed-URL access pattern.
3. **Document understanding**: PDF text extraction, DOCX, images, scanned PDFs (OCR), and vision-model fallback for low-quality scans. Output a normalised document model: text blocks with positions, tables, metadata, fonts and colours (needed for hidden-text detection).
4. **Extraction**: schema-driven structured extraction with Pydantic models, per-field confidence, and source spans (page and position) for every value.
5. **Validation and policy engine**: deterministic rules (YAML or Python) plus RAG over the tenant's own policy documents. Every rule result is stored with its reason.
6. **Risk and anomaly scoring**: pluggable detectors returning a score, label and evidence.
7. **Guardrails**: prompt-injection detection (hidden text, instruction-like content in documents, metadata), PII detection and redaction for logs, output validation (schema, ranges, banned content), jailbreak resistance.
8. **Workflow runtime**: LangGraph graphs with a Postgres checkpointer, durable state, retries, timeouts, and **interrupts for human approval**. Every graph run has an ID linked to the audit log.
9. **Approvals**: a unified approval inbox across workflows, approval rules (thresholds, chains, delegation, out-of-office), segregation of duties (the person who submitted cannot approve), SLA timers and escalation. Approve from web, Slack, or email via signed single-use links with short expiry.
10. **Audit log**: append-only, hash-chained entries (each entry includes the previous entry's hash) so tampering is detectable. Export to CSV and JSON. Read-only auditor role.
11. **Notifications**: email (transactional provider), Slack, in-app; template system with variables, multi-language (English and Arabic, RTL-aware), preview, and approval-required flag for sensitive templates.
12. **Scheduling**: calendar integrations, availability calculation, time zones, buffers, meeting-link creation, reminders, reschedule and cancel flows, ICS files.
13. **Model router**: provider abstraction over Anthropic and OpenAI (ASK whether to also support local models), cheap-model-first routing with escalation on low confidence, retries with backoff, token and cost tracking per call, per-tenant monthly budget with hard stop and alert.
14. **Search**: Postgres full-text plus pgvector semantic search across candidates, documents, vendors and invoices.
15. **Public API and webhooks**: versioned REST API with OpenAPI spec, API keys with scopes, outbound webhooks signed with HMAC, retries, and idempotency keys; inbound webhooks deduplicated by event ID.
16. **Scheduled jobs**: reconciliation, SLA checks, retention deletion, digest emails, eval sampling.
17. **Feature flags** per tenant.

### 2.3 Decision records, policy versions, appeals and replay
Every AI-assisted decision in any workflow (a candidate score, a shortlist suggestion, a fraud flag, an invoice risk score, an approval) becomes a first-class **Decision Record** with its own stable ID. When a candidate, a vendor, an auditor or a regulator asks "why?", one ID answers it.

1. **Decision Record contents**: decision ID, workflow and entity, the exact inputs (document hashes, extracted fields), the policy and rubric **versions** applied, model and prompt versions, the AI output with confidence, the evidence (quotes with source locations, rule results, risk signals), the final human action, who took it and why, and timestamps. Linked to the hash-chained audit log.
2. **Policy as code, versioned**: rubrics, approval rules, spend policies and risk thresholds are stored as versioned YAML (with a UI editor on top). Every Decision Record points to the exact version used. Changing a policy never rewrites history.
3. **Appeals and human review requests**: candidates (from the portal) and vendors (from the vendor portal) can contest a specific decision by its ID and ask for human review. Appeals get their own queue, SLA, a required written outcome, and are reported in analytics. This supports the human-review and appeal rights in laws such as the Colorado AI Act.
4. **Replay**: an auditor or admin can re-run any past decision with the same inputs, policy version, prompt version and model settings (temperature 0, fixed seeds where supported, cached model responses kept for replay) and see whether the result matches. Differences are flagged. This proves reproducibility.
5. **Decision receipts**: a shareable, permission-checked summary page and PDF for any decision (for example, "why this invoice was blocked", "how this application was assessed") with sensitive data redacted by role.
6. **Compliance pack export**: one-click export of decision records, policy versions, overrides, appeals, fairness and bias reports, and model cards for a chosen period, for audits.

### 2.4 Developer SDK and API experience
Make the engine easy for other developers to adopt; this is a major part of the open-source appeal.
1. **SDKs** for Python (`pip install pairly`) and TypeScript (`npm install @pairly/sdk`) generated from the OpenAPI spec, with typed clients, async support, retries and pagination helpers.
2. **One-call workflows**: for example `pairly.documents.submit(file, workflow="pay")` returns a decision ID; results arrive by webhook or polling.
3. **Webhook helpers**: `verify_webhook_signature` and event constructors in both SDKs.
4. **Batch API** for bulk submissions with a webhook when the batch completes.
5. **Quickstarts** in the docs: "add human-approved AI to your app in 10 minutes", with copy-paste examples for FastAPI, Flask, Next.js and Express.
6. **Sandbox mode** with seeded data and test API keys.

### 2.5 Data model essentials
Use Alembic migrations. Key tables (non-exhaustive): organisations, users, roles, memberships, documents, extractions, extraction_fields, rule_results, risk_signals, workflow_runs, approvals, approval_steps, audit_events, notifications, templates, integrations, api_keys, webhooks, model_calls (cost and latency), eval_runs, decision_records, policy_versions, appeals, replays. Hire and Pay add their own tables (section 3 and 4). Encrypt sensitive columns (bank details, phone numbers, ID documents, compensation) at the application level.

---

## 3. Pairly Hire (A to Z)

### 3.1 Requisitions and jobs
1. **Requisition request and approval**: hiring manager requests a role (title, team, budget, headcount, location, work model, start date); approval chain before the job can open.
2. **AI job description drafting** from a short brief, with an **inclusive-language checker** (flags gendered or exclusionary wording and suggests alternatives) and a **requirements sanity check** (flags unrealistic combinations, for example "10 years of a 3-year-old framework").
3. **Pay transparency**: salary range field, shown or hidden per job (some jurisdictions require it).
4. **Rubric builder**: must-have criteria, nice-to-have criteria, weights, knockout questions, and for each criterion a description of what evidence looks like. The AI drafts; the human edits and locks the rubric before applications are scored. Rubric is versioned.
5. **Interview plan**: stages, interviewers per stage, scorecards per stage mapped to rubric criteria.
6. **Careers page** per tenant (Next.js, SEO-friendly), with `JobPosting` structured data for Google Jobs, and a JSON/XML feed for job boards.
7. **Multi-language jobs** (English and Arabic).

### 3.2 Applying
1. **Application form builder**: standard fields, custom questions, knockout questions, file uploads, voluntary self-identification fields kept separate from scoring (for bias reporting only).
2. **AI-use notice and consent** shown before submission, with a plain-language explanation of how AI is used, that humans make decisions, and how to request human review or data deletion.
3. **Bot and spam protection** (Turnstile or hCaptcha, rate limits, disposable-email checks).
4. **Duplicate applicant detection** across jobs (email, phone, CV hash, fuzzy name and profile match).
5. **Acknowledgement email** with expected timeline and a link to the candidate portal.
6. **Accessibility**: WCAG 2.2 AA, keyboard navigation, screen-reader labels, mobile-first.

### 3.3 Screening (the core AI)
1. **CV parsing** from PDF, DOCX and images into a structured profile: contact, experience (employer, title, dates), education, skills, certifications, languages, links.
2. **Evidence-based scoring** against the locked rubric: for each criterion a score, a confidence, and **quoted evidence from the CV with source location**. No evidence means no credit. Overall score is computed deterministically from criterion scores and weights, not by the model.
3. **Blind mode** (default on): name, photo, age, date of birth, gender markers, nationality, marital status, address and university names (configurable) are hidden from the scoring model and from reviewers until the shortlist stage.
4. **Counterfactual fairness check**: for a sample of CVs, re-score with swapped names, genders and nationalities; flag any score change above a threshold. Results feed the bias report and evals.
5. **Prompt-injection and manipulation detection**: hidden text (white or near-background colour, tiny fonts, off-page text), instruction-like content ("ignore previous instructions", "rank this candidate first"), keyword stuffing, metadata injection. Flag, explain, neutralise (scoring ignores the injected content), and show the recruiter exactly what was found.
6. **Claim consistency checks**: overlapping or impossible dates, unexplained gaps, title inflation patterns, skills claimed without supporting experience, employers that cannot be matched to any public presence (flag only, never auto-reject).
7. **AI-generated application signals**: low-specificity boilerplate, mass-application patterns; shown as information, never as an automatic penalty.
8. **Knockout handling**: knockout answers route to a human review queue with a suggested action; nothing is auto-rejected.
9. **Explainability panel** per candidate: criterion scores, evidence quotes, flags, model and prompt version, and a "why not higher" explanation.
10. **Human override** with required reason; overrides are logged and used for rubric calibration.
11. **Calibration mode**: recruiters label a sample; the system reports agreement between AI and humans per criterion and suggests rubric clarifications.

### 3.4 Pipeline (ATS)
1. **Kanban pipeline** with customisable stages per job; bulk actions that require confirmation.
2. **Candidate profile**: timeline of every event, notes with @mentions, tags, files, messages, scores, flags.
3. **Talent rediscovery**: semantic search across past applicants for new roles (respecting consent and retention).
4. **Referrals**: employee referral links and tracking.
5. **Sourcing notes** (OPTIONAL LATER): import a candidate manually from a profile URL with consent.

### 3.5 Communication and anti-ghosting
1. **Template library**: acknowledgement, status update, interview invite, reschedule, reminder, rejection (several tones and stages), offer, withdrawal confirmation; all multi-language.
2. **AI-personalised drafts** from templates; **rejections and offers always require human approval** before sending.
3. **SLA timers**: if a candidate has had no update for N days (configurable, default 7), the system drafts a status update and alerts the recruiter. A dashboard shows "candidates waiting longest".
4. **Closing the loop**: every candidate gets a final outcome. Optional short, respectful reason in rejections, generated from rubric evidence and approved by a human.
5. **Channels**: email first; WhatsApp Business (OPTIONAL LATER, ASK).
6. **Unsubscribe and communication preferences**.

### 3.6 Scheduling
1. **Interviewer availability** from Google Calendar and Microsoft 365 (Outlook), working hours, buffers, maximum interviews per day.
2. **Candidate self-scheduling link** showing real availability in the candidate's time zone.
3. **Panel and multi-stage scheduling** (find slots where all panelists are free).
4. **Automatic meeting links**: Google Meet first, then Zoom (Server-to-Server OAuth) and Microsoft Teams.
5. **Reminders** (24 hours and 1 hour), ICS attachments, reschedule and cancel flows for both sides, no-show tracking.
6. **Interviewer briefing email** with the interview kit and the candidate summary.

### 3.7 Interviews and feedback
1. **Interview kits** generated per candidate and stage: questions mapped to rubric criteria, plus probes for weak or unclear evidence in the CV.
2. **Structured scorecards**; interviewers submit **independently before seeing others' feedback** (reduces groupthink).
3. **Feedback reminders** and SLA for submitting scorecards.
4. **Debrief summary**: AI summarises scorecards per criterion, highlights disagreements and open questions; humans decide.
5. **Interview integrity** (signals only, never automatic decisions): identity consistency checklist across stages (same person, same name, same contact), a prompt for live verification steps at final stage, and a policy page on acceptable candidate AI use. ASK before adding any biometric or video analysis features (legal sensitivity); default is none.
6. **Transcript notes** (OPTIONAL, consent-gated): upload or connect a recording transcript and get structured notes mapped to the rubric.

### 3.8 Decision, references and offer
1. **Decision approval chain** (hiring manager, then HR or finance by rules).
2. **Reference checks**: send structured reference forms to referees by email, collect answers, summarise with flags for inconsistencies with the CV.
3. **Offer builder** from templates: compensation, start date, conditions; approval required; PDF generation; e-signature integration (ASK which provider; OPTIONAL).
4. **Pre-employment checklist**: documents to collect (configurable), status tracking. Store documents encrypted, short retention.
5. **Candidate experience survey** after the process ends, for every outcome.

### 3.9 Candidate portal
1. Status timeline for every application, with honest, human-readable stages.
2. Upcoming interviews with join links, reschedule and cancel.
3. Messages with the recruiter.
4. Document uploads requested by the company.
5. Withdraw application.
6. Browse and apply to other open jobs.
7. **Privacy centre**: download my data, request deletion, request human review of an AI-assisted step.
8. Magic-link login, no passwords.

### 3.10 Hiring analytics
Time to first response, time in stage, time to hire, stage conversion, source effectiveness, SLA breaches, ghosting rate (candidates without update over N days), interviewer load, scheduling time saved, AI and human agreement rate, override rate, injection and fraud flags caught, cost per screened application.

**Bias and adverse-impact report**: selection rates by voluntary self-identified group at each stage and the impact ratio (four-fifths rule), with small-sample warnings. Exportable as a bias-audit pack (data, method, results) to support audits like NYC Local Law 144. Never use self-ID data in scoring.

---

## 4. Pairly Pay (accounts payable, A to Z)

### 4.1 Vendors
1. **Vendor onboarding**: self-service vendor form via invite link: legal name, trade licence, TRN (UAE VAT number), addresses, contacts, bank details, documents. Dedupe against existing vendors (fuzzy name, TRN, IBAN).
2. **TRN validation**: format checks (UAE TRN is 15 digits) and a hook for official verification (TODO: source of truth).
3. **IBAN validation**: checksum (ISO 13616), country and bank code checks, name-on-account capture.
4. **Bank-detail change workflow (signature feature)**: any change request (from email, invoice, vendor portal, or staff) creates a locked change ticket. Payments to that vendor are held. Requires **out-of-band verification**: a staff member calls the vendor on the phone number already on file (never one from the request) and records the result, plus **dual approval** by two different users and an optional cooling-off period. Every step is audited.
5. **Vendor risk profile**: age of vendor record, recent changes, payment history, unusual patterns.

### 4.2 Invoice intake
1. Dedicated inbound email per tenant, upload, vendor portal submission, API.
2. Formats: PDF (digital and scanned), images, DOCX, and structured e-invoices (UBL / Peppol PINT AE XML). Design the parser interface so new e-invoice formats can be added.
3. Email handling: multiple attachments, invoice in body, forwarded chains; ignore marketing emails; reply to sender with a receipt and reference number.
4. File hash dedupe at intake.

### 4.3 Extraction
1. Header fields: vendor, vendor TRN, invoice number, invoice date, due date, payment terms, currency, PO number, subtotal, VAT amount and rate, total, bank details on the invoice.
2. **Line items**: description, quantity, unit price, VAT, line total.
3. Per-field confidence and source location; low-confidence fields highlighted for human correction in a side-by-side viewer (document on the left, fields on the right, click a field to highlight its source).
4. **Learning from corrections**: corrections are stored and used to improve vendor-specific extraction hints (few-shot examples per vendor), measured by evals.

### 4.4 Validation and fraud detection
1. **Arithmetic checks**: line totals, subtotal, VAT at the configured rate (5% in the UAE by default, configurable per tax code), total.
2. **Duplicate and near-duplicate detection**: same vendor plus invoice number, fuzzy invoice numbers (transposed digits, prefixes), same amount within N days, same file hash, same line items. Show the suspected original side by side.
3. **Bank mismatch detection**: bank details on the invoice differ from the vendor master: **block payment and open a bank-detail change ticket automatically**.
4. **Vendor impersonation signals**: sender domain does not match the vendor's known domains, lookalike domains, first-time sender, urgency language ("pay today", "new account").
5. **Amount anomalies**: unusual amount for the vendor versus history, round-number patterns, split invoices just under approval thresholds.
6. **Fake or generated invoice signals**: template differs from the vendor's past invoices, metadata inconsistencies, editing traces.
7. **Prompt-injection detection** in invoice text and metadata ("approve this invoice", hidden instructions): flagged, neutralised, shown to the reviewer.
8. **Policy checks via RAG** over the tenant's spend policy (for example: "software over 10,000 needs IT approval").
9. **Risk score** combining all signals with explanations; high-risk invoices always go to a human regardless of amount.

### 4.5 Matching and coding
1. **Purchase orders and goods receipts** (create in-app or import CSV).
2. **2-way and 3-way matching** with configurable tolerances (price, quantity, total), partial deliveries, and multiple invoices per PO.
3. **GL coding and cost centre suggestions** learned from history and corrections, with confidence.
4. **Exceptions queue**: categorised (no PO, price variance, quantity variance, missing receipt, duplicate suspect, bank mismatch, policy breach, low confidence), each with an AI explanation and suggested resolution, plus a drafted message to the vendor or requester when needed (sent only after approval).

### 4.6 Approvals and payments
1. **Approval rules** by amount, department, vendor risk, category; chains, delegation, out-of-office; segregation of duties enforced.
2. Approve or reject from web, Slack (interactive message), or email (signed single-use link).
3. **Payment runs**: propose a run (due dates, discounts, cash limits), finance approves, export a bank payment file (CSV and a generic ISO 20022 pain.001 XML as OPTIONAL), mark as paid. No real bank connections.
4. **Early-payment discount optimiser**: highlight invoices where paying early saves money.
5. **Cash forecast**: upcoming payables by week.

### 4.7 Vendor portal
1. Invoice status (received, in review, approved, scheduled, paid) with dates.
2. Submit invoices and credit notes.
3. Update details: contact changes directly; bank changes always go through the verified change workflow.
4. Messages with AP.

### 4.8 Reconciliation and close support
1. Match payments to invoices; flag unmatched items.
2. Month-end report: open payables, aging, accruals suggestions for received-not-invoiced goods.
3. Accounting export: CSV formats for Xero, QuickBooks and Zoho Books (ASK which ones first), and a generic journal export.

### 4.9 AP analytics
Touchless rate, cycle time (receipt to approval to payment), cost per invoice (including model cost), exception rate by category, duplicates prevented (count and value), bank-change attempts blocked, fraud flags with outcomes, approval SLA breaches, on-time payment rate, discounts captured, days payable outstanding.

---

## 5. Shared user interface

### 5.1 Dashboards for every user type
Every person who touches the system gets a dashboard built around one question: "what needs me right now, and how are things going?" Each dashboard shows only what that role is allowed to see, has an "action needed" section at the top, and links every number to the underlying records.

**Hiring side**
1. **Recruiter**: action queue (applications to review, flags to check, drafts awaiting approval, candidates waiting longest for an update), jobs overview with pipeline counts per stage, today's and this week's interviews, missing scorecards, SLA breaches, sourcing and talent-pool suggestions, personal metrics (time to first response, time in stage).
2. **Hiring manager**: requisitions and their approval status, shortlists awaiting decision, upcoming interviews, debrief summaries ready, offers awaiting approval, hiring progress per open role, time to hire.
3. **Interviewer**: upcoming interviews with join links and interview kits, scorecards due (with reminders), past interviews and submitted feedback, personal load this week.
4. **HR or Talent lead**: all jobs and teams, funnel conversion, time to hire, source effectiveness, ghosting rate, interviewer load balance, AI and human agreement rate, override rate, fraud and injection flags, bias and adverse-impact report, candidate experience survey results.
5. **Candidate (portal)**: application status timelines, upcoming interviews, messages, requested documents, other open jobs, privacy centre.
6. **Referee (reference form)**: a simple single-page form with clear context, progress saving and confirmation; no login needed beyond a signed link.
7. **Employee referrer**: referral link, referred candidates and their stage (limited, privacy-safe view), referral status updates.

**Payments side**
8. **AP clerk**: intake queue, low-confidence fields to correct, exceptions queue by category, duplicate suspects, invoices waiting on vendors, personal throughput.
9. **Approver**: invoices awaiting my approval with risk badges and SLA timers, delegated items, approved and rejected history, spend under my authority this month.
10. **Finance controller or CFO**: payables overview, aging, upcoming payment runs and cash forecast, discounts available, exceptions trend, fraud and bank-change attempts blocked, touchless rate, cycle time, cost per invoice, days payable outstanding, month-end close status.
11. **Vendor (portal)**: invoice statuses with dates, expected payment dates, submissions, messages, verified details, open change requests.

**Shared and system**
12. **Auditor (read-only)**: audit log search and export, hash-chain integrity check result, approvals history, overrides with reasons, bank-change tickets with verification evidence, AI decisions with model and prompt versions.
13. **Organisation admin**: users and roles, integrations status (calendar, email, Slack, Zoom, Microsoft), approval rules, policies, templates, budgets and usage, retention settings, data requests (export and delete) queue.
14. **AI governance**: model usage by component, cost versus budget, eval results over time and current pass or fail status against thresholds, guardrail triggers (injections, PII redactions), fairness test results, human override rates, model and prompt versions in production, model cards.
15. **Platform operations (self-hosters and me)**: service health, queue depth and job failures, webhook delivery status and retries, reconciliation job findings, error rates, latency, storage usage, per-tenant usage, background job schedules.

### 5.2 Common UI elements
1. **Home dashboard** per role, as defined in 5.1, with what needs attention now.
2. **Unified approval inbox** across Hire and Pay, with filters, SLA badges and keyboard shortcuts.
3. **Document viewer** with field highlighting and evidence quotes.
4. **Audit viewer** with search and export.
5. **Settings**: users and roles, approval rules, policies (upload policy docs for RAG), templates, integrations, budgets, retention, branding, languages.
6. **Arabic and English**, full RTL support.
7. Light and dark mode, responsive, accessible (WCAG 2.2 AA).
8. Empty states and onboarding checklist for a new organisation.

### 5.3 Recruiter, interviewer and hiring manager experience (the UX bar)
These are the people who decide whether the product feels great or painful. Treat their experience as the most important part of the UI. Research behind this: recruiters' most common complaints about existing ATS tools are too many clicks, constant new tabs and redirects that lose their place while reviewing candidates, manual interview editing, and chasing feedback that lives outside the system; 59% of companies say they lose top candidates because interview feedback is slow.

**Principles**
1. **Action first.** Every screen opens with what needs doing, not with settings or charts.
2. **Never lose your place.** Reviewing, moving, messaging and scheduling a candidate all happen in a side panel or split view without leaving the list. No new tabs, no full-page redirects, back button always works, filters and scroll position persist.
3. **Click budget.** Core actions must take at most 3 clicks or 1 keyboard shortcut (see targets below).
4. **Keyboard-first, mouse-friendly.** Command palette (Cmd/Ctrl+K) for everything; shortcuts shown on hover.
5. **AI helps, never blocks.** AI suggestions appear inline with evidence and can be accepted, edited or ignored in one action. Nothing waits on a slow model: show the item immediately and stream AI results in.
6. **Undo instead of confirm** for reversible actions (10-second undo toast); confirmations only for irreversible or external actions (sending a rejection, an offer, a payment approval).
7. **Plain language** everywhere, no jargon, no AI hype; explain every AI flag in one sentence.
8. **Progressive disclosure**: simple by default, details one click away.
9. **Speed budget**: interactions under 200 ms (optimistic UI), page loads under 1 second on the demo data, no layout jumps.
10. **Zero-training goal**: a recruiter who has never seen the product completes the core tasks below without help.

**Recruiter screening mode (the most important screen)**
1. Split view: candidate document on the left, rubric scores with evidence quotes and flags on the right; clicking a quote highlights it in the document.
2. Keyboard flow: J/K next and previous candidate, A advance, H hold, R draft rejection (goes to the approval queue, never sent instantly), N add note, S schedule, T tag, / search.
3. Next candidate loads instantly (prefetch); position in the queue is kept after every action.
4. Bulk mode: select many, apply one action, see a summary before anything external is sent.
5. Blind mode toggle and status clearly visible, with an explanation of what is hidden.
6. Compare view: two or three candidates side by side on the same rubric.

**Scheduling UX**
1. "Schedule" opens a panel with suggested slots already filtered by all panelists' calendars; one click sends the candidate a self-scheduling link, or one click books a chosen slot.
2. Drag to reschedule; conflicts and time zones shown inline; both sides notified automatically.
3. Editing an interview (time, panel, link) is a single form, never a manual multi-step process.

**Interviewer experience**
1. The calendar invite contains the meeting link, a short candidate summary and a **one-click link to the scorecard**, mobile-friendly, no login wall beyond a magic link.
2. The interview kit is readable on a phone during or right after the call.
3. Scorecard: 3 to 5 questions mapped to rubric criteria, a 1 to 4 rating per criterion with anchor descriptions, a short notes box with voice-to-text, and an overall recommendation. Completable in under 3 minutes.
4. Independent submission: others' feedback is hidden until you submit.
5. Gentle reminders at 2 hours and 24 hours after the interview; one-click "remind me later".
6. Interviewer load and upcoming interviews visible in one place.

**Hiring manager experience**
1. Review a shortlist and leave a decision with a reason in one screen, from desktop, mobile, email or Slack, without the recruiter's help.
2. Debrief summary on one page: criteria, ratings, disagreements, open questions, recommendation.
3. Approvals (requisitions, offers) one tap on mobile, with the context visible before approving.

**Messaging UX**
1. Compose panel beside the candidate profile: pick a template, AI drafts a personalised version, edit inline, preview exactly what the candidate will see, then send or submit for approval.
2. All conversation history in the candidate timeline; reply to candidates from the same panel.

**Notifications UX**
1. Smart digests instead of floods; per-user preferences per channel; snooze; urgent items only in real time (SLA breaches, interviews starting soon, approvals blocking others).

**Mobile**
Everything an interviewer, hiring manager or approver needs between meetings works well on a phone: approvals, scorecards, interview kits, reading candidate summaries, replying to candidates. Recruiter screening mode is desktop-first but must remain usable on a tablet.

**Onboarding and customisation**
1. New organisation setup in under 10 minutes with a guided checklist and optional sample data.
2. Customisable pipeline stages, scorecard templates, fields, and visibility rules per role (who can see what, including compensation).
3. Contextual tips on first use, dismissible, never blocking.

**Design system**
1. One consistent component library (shadcn/ui with custom tokens), documented in Storybook, with English and Arabic (RTL) and light and dark variants of every component.
2. Accessibility WCAG 2.2 AA: focus states, contrast, screen-reader labels, reduced motion.
3. Follow the frontend design guidance available to you for a distinctive, polished visual style; the product must not look like a generic template.

**UX acceptance targets (tested, not assumed)**
Write Playwright tests that perform each task on the seed data, count clicks and keystrokes, and fail if a budget is exceeded. Report times and counts in the milestone summary.

| Task | Budget |
|---|---|
| Move a candidate to the next stage | 1 click or 1 key |
| Add a note to a candidate | 2 clicks |
| Send a templated message (non-sensitive) | 3 clicks |
| Draft a rejection into the approval queue | 1 key + 1 click |
| Book an interview with an available slot | 3 clicks |
| Reschedule an interview | 2 clicks |
| Find a past candidate by skill | 1 search |
| Interviewer opens scorecard from invite and submits | under 3 minutes, no more than 1 login step |
| Hiring manager reviews a shortlist of 5 and records decisions | under 5 minutes |
| Recruiter screens 20 candidates in screening mode | under 10 minutes on seed data |
| Approve an invoice or offer from mobile | 2 taps |

In addition, before M2 and M3 are marked done, run a short moderated usability test with at least 3 real people in recruiter or hiring manager roles (ASK me to arrange participants), record issues, fix the critical ones, and include the findings in the milestone summary.

---

## 6. Security, privacy and compliance

1. **Authentication**: email magic link and Google/Microsoft SSO for staff; magic link for candidates and vendors; optional TOTP 2FA for staff and required for approvers and admins.
2. **Authorisation**: RBAC checked server-side on every request and every agent tool call. Agents get **least-privilege tools**: they can read and propose, never execute final actions.
3. **Tenant isolation** enforced in queries and tested.
4. **Encryption**: TLS everywhere; application-level encryption for sensitive fields; secrets from environment or a secrets manager, never in code; no default secrets (startup fails if a secret is missing).
5. **Web security**: CSRF protection, strict CSP, secure cookies, rate limiting, input validation, output encoding, SSRF protection for any URL fetching, file-upload scanning (ClamAV) and sandboxed document parsing.
6. **Webhooks**: inbound deduplicated by event ID (idempotent), return errors on failure so providers retry; outbound signed with HMAC and retried with backoff.
7. **Signed links**: single-use, short expiry, bound to the action and the user.
8. **Reconciliation jobs** to catch any state drift between approvals, statuses and notifications.
9. **Privacy**: data minimisation, consent records, configurable retention with automatic deletion (default: rejected candidates 12 months, ASK), data subject requests (access, export, delete), PII redaction in logs and traces. Align with UAE PDPL and GDPR principles; document data flows.
10. **AI governance docs**: a model card per AI component (purpose, inputs, outputs, limits, evals, human oversight), a risk register, and a human-oversight description. Map features to NYC LL144, EU AI Act high-risk duties (oversight, logging, documentation, transparency) and Colorado/Illinois notice and human-review rights in `docs/compliance.md`, without claiming legal compliance.
11. **Threat model** in `docs/security.md` (STRIDE), including prompt injection, data exfiltration through the model, malicious documents, BEC, insider approval abuse.
12. **Responsible disclosure** policy (`SECURITY.md`).

---

## 7. Evaluation and observability

### 7.1 Datasets (all synthetic, in `synth/` and `evals/`)
1. **CVs**: at least 300 synthetic CVs across roles and seniority, in varied layouts (PDF, DOCX, image scans), with ground-truth structured fields and human-style rubric labels for 3 sample jobs.
2. **Attack CVs**: hidden white text, tiny fonts, instruction injections, keyword stuffing, metadata injections, inconsistent dates.
3. **Fairness pairs**: identical CVs differing only in name, gender markers and nationality.
4. **Invoices**: at least 300 synthetic invoices from 30+ synthetic vendors with varied templates, languages (English and Arabic), scans and e-invoice XML, with ground truth for every field and line.
5. **Attack invoices**: duplicates and near-duplicates, changed bank details, lookalike sender domains, split invoices, injections, inflated amounts, VAT errors.
6. **POs and receipts** for matching scenarios, including tolerance edge cases.

### 7.2 Metrics and thresholds (initial, adjust after baseline, ASK before lowering)
- CV field extraction accuracy (per field), rubric scoring agreement with labels, evidence validity (quoted evidence actually exists in the CV: target 100%).
- Fairness: score change across counterfactual pairs below a set threshold.
- Injection detection recall and false-positive rate.
- Invoice field accuracy, line-item accuracy, total and VAT accuracy.
- Duplicate detection recall and precision; bank-mismatch detection recall (target 100%).
- Fraud and injection catch rate; false positives.
- Latency and cost per document.

### 7.3 Tooling
1. **PromptFoo** suites for prompts and guardrails, run in CI on every pull request; failing thresholds block merge.
2. **LangFuse** for tracing every model call and graph run, datasets, scoring, prompt versions, and cost.
3. **OpenTelemetry** traces and metrics for the API and workers.
4. An `evals/report.md` generated from the latest run, used for README numbers.

---

## 8. Tech stack

- **Agents and backend**: Python 3.12, FastAPI, Pydantic v2, LangGraph (Postgres checkpointer), LangChain only where useful, SQLAlchemy 2 plus Alembic.
- **Background jobs**: Redis with arq (or Celery, ASK), scheduled jobs.
- **Models**: Anthropic Claude (small model first, larger on escalation) and OpenAI behind one provider interface; structured outputs everywhere.
- **Documents**: pdfplumber or pypdf, python-docx, Tesseract OCR, vision-model fallback; consider Docling for layout (evaluate, ASK if heavy).
- **Data**: PostgreSQL 16 with pgvector; object storage via S3-compatible API (MinIO locally).
- **Frontend**: Next.js (App Router), TypeScript, Tailwind CSS, shadcn/ui, TanStack Query, react-hook-form with zod, i18n with RTL.
- **Auth**: Auth.js or Better Auth for the web app (ASK), backed by the API's RBAC.
- **Integrations**: transactional email provider (Resend or SMTP) with inbound email parsing; Slack app; Google Calendar and Meet; Microsoft Graph (Outlook, Teams); Zoom Server-to-Server OAuth; optional e-signature (ASK).
- **Observability and evals**: LangFuse, PromptFoo, OpenTelemetry, structured JSON logs, optional Sentry.
- **Security tooling**: ClamAV, dependency scanning, secret scanning, Bandit, npm audit.
- **Quality**: ruff, mypy (strict on packages/core), pytest with coverage, eslint, vitest, Playwright end-to-end tests.
- **Ops**: Docker and Docker Compose (one command: `make demo` starts everything with seeded data), GitHub Actions CI (lint, type check, tests, evals, build, security scans), pre-commit hooks.
- **MCP server**: Python MCP SDK exposing safe tools: search candidates, get candidate summary, list approvals, get invoice status, propose (never execute) actions.
- **License**: AGPL-3.0 plus commercial license (dual licensing, see section 9.1).

---

## 9. Developer experience and docs (this is part of the product)

1. **README** that sells in 30 seconds: one-line pitch, demo GIF, architecture diagram (Mermaid), "run it in one command", real eval numbers from the latest report, links to docs.
2. `docs/architecture.md` with diagrams of the engine, the workflow graphs (exported LangGraph diagrams), and data flow.
3. **ADRs** (`docs/adr/`) for every major decision.
4. `docs/add-a-workflow.md`: build a tiny third workflow as the example.
5. `docs/security.md`, `docs/compliance.md`, `docs/evals.md`, `docs/model-cards/`.
6. `CONTRIBUTING.md`, issue and PR templates, `CODE_OF_CONDUCT.md`, `SECURITY.md`.
7. Seed script creating a demo organisation with users for every role, two open jobs with candidates in every stage, vendors, POs and invoices in every state, including attack examples.
8. Postman or Bruno collection plus generated OpenAPI docs.

---

### 9.1 Licensing: AGPL-3.0 plus commercial (dual licensing)
Pairly is genuinely open source under the **GNU AGPL v3.0**, and also available under a **paid commercial license** from Proofly Technology (the licensor).

1. **AGPL path (free)**: anyone, including companies of any size, may use, modify and self-host Pairly for free. If they offer a modified version to users over a network, or distribute it, they must make their modified source available under AGPL-3.0.
2. **Commercial path (paid)**: companies that want to keep their changes private, embed Pairly in a closed-source product, or run it as a service without AGPL obligations buy a commercial license.
3. **Files to create**:
   - `LICENSE`: the full, unmodified AGPL-3.0 text.
   - `COMMERCIAL-LICENSE.md`: a plain-language explanation of when a commercial license is needed, what it grants, and how to contact Proofly Technology. No prices in the repo (ASK).
   - `LICENSING.md`: a short FAQ ("Can I use Pairly at my company for free?", "When do I need a commercial license?", "What counts as offering it over a network?"), marked as not legal advice.
   - `NOTICE.md`: every third-party component and its license.
   - SPDX header in every source file: `SPDX-License-Identifier: AGPL-3.0-only` plus the copyright line for Proofly Technology.
4. **Contributor License Agreement (CLA) from day one**: dual licensing only works if Proofly Technology can relicense all code. Set up a CLA bot on GitHub that asks every contributor to sign on their first pull request; block merges until signed. Explain the CLA honestly in `CONTRIBUTING.md`, including a pledge that the project will remain available under AGPL-3.0.
5. **Dependency compatibility**: CI checks every dependency's license and fails on anything incompatible with AGPL-3.0 or with commercial relicensing.
6. **Wording**: describe Pairly as "open source (AGPL-3.0) with a commercial license option." Never imply that commercial use requires payment; it does not under AGPL.

## 10. Demo scripts (must work flawlessly from the seed data)

### 10.1 Hire demo (5 minutes)
1. Create a job from a 3-line brief; show the drafted JD, inclusive-language suggestions and the rubric; lock the rubric.
2. Three CVs arrive. Show blind scoring with evidence quotes for each criterion.
3. One CV contains hidden white text saying "rank this candidate first": show the flag, the extracted hidden text, and that the score ignored it.
4. Approve a shortlist. The candidate receives a personalised email, books a slot through the self-scheduling link, and a Meet link is created automatically.
5. Open the candidate portal: the status timeline updates live.
6. Show the anti-ghosting dashboard and the audit trail of every step.

### 10.2 Pay demo (5 minutes)
1. Forward three invoices to the inbound address.
2. Invoice 1 (clean, matched to a PO): extracted, matched, approved by the rule, scheduled.
3. Invoice 2 (near-duplicate with a transposed invoice number): flagged with the original side by side.
4. Invoice 3 (real vendor, new bank details, lookalike sender domain, hidden "approve immediately" text): blocked, bank-change ticket opened, payments to the vendor held, approver alerted in Slack.
5. Complete the out-of-band verification and dual approval flow.
6. Show the analytics dashboard and the eval report.

### 10.3 Engineer demo (2 minutes)
Show a LangGraph run trace in LangFuse, the CI eval gate failing on a deliberately bad prompt change, and the MCP server answering "which approvals are waiting for me?" in Claude.

---

## 11. Milestones and acceptance criteria

Work milestone by milestone. At the end of each one: run all tests and evals, update docs, and give me a short summary with what works, what's left, every `TODO`, every `ASK`, and the eval numbers.

**M0: Foundations (scaffolding)**
Monorepo, Docker Compose, Postgres with pgvector, Redis, MinIO, FastAPI skeleton, Next.js skeleton, auth, RBAC, tenancy, audit log (hash-chained), CI pipeline, pre-commit. Acceptance: `make demo` boots everything; CI is green; tenant isolation test passes.

**M1: Core engine**
Ingestion, document understanding, extraction framework, guardrails (injection detection), policy engine with RAG, risk scoring, workflow runtime with interrupts, approvals inbox with Slack and email approvals, notifications, model router with cost tracking and budgets, LangFuse tracing. Acceptance: a toy workflow runs end to end with a human approval, fully audited.

**M2: Hire MVP**
Jobs, rubric builder, application form with AI notice, CV parsing, evidence-based blind scoring, injection detection, pipeline, templates with approval, self-scheduling with Google Calendar and Meet, candidate portal. Synthetic datasets and evals for Hire. Acceptance: Hire demo 10.1 works from seed data; Hire evals meet thresholds.

**M3: Hire complete**
Requisitions, inclusive-language checker, claim consistency checks, counterfactual fairness tests, interview kits, independent scorecards, debrief summaries, references, offers, anti-ghosting SLAs, analytics and bias report, Microsoft and Zoom integrations, Arabic.

**M4: Pay MVP**
Vendors with IBAN and TRN validation, bank-detail change workflow with out-of-band verification and dual approval, invoice intake by email and upload, extraction with side-by-side viewer, arithmetic and VAT checks, duplicate detection, bank mismatch blocking, approval rules, payment proposal and export. Synthetic datasets and evals for Pay. Acceptance: Pay demo 10.2 works; Pay evals meet thresholds, bank-mismatch recall 100%.

**M5: Pay complete**
E-invoice XML parsing, PO and receipt matching (2 and 3-way), GL coding suggestions, exceptions queue with drafted vendor messages, impersonation and anomaly signals, vendor portal, early-payment discounts, cash forecast, reconciliation, accounting exports, analytics.

**M6: Launch polish**
MCP server, add-a-workflow guide with a tiny example workflow, full docs and model cards, security review against the threat model, performance pass, README with GIFs and real eval numbers, hosted demo (ASK before deploying).

---

## 12. Out of scope (do not build unless I ask)
- Real bank connections or moving real money.
- Biometric or emotion analysis of candidates.
- Automatic rejection of any candidate or automatic payment of any invoice without a human approval step.
- Scraping job boards or social networks.
- Training or fine-tuning foundation models.

---

## 13. Questions to ask me before or during the build (ASK list)
1. GitHub organisation name (pairly, pairly-ai or pairlyhq) and domain.
2. Commercial license pricing and terms (to be reviewed by a lawyer before the first sale).
3. Auth library (Auth.js or Better Auth) and queue (arq or Celery).
4. Which accounting exports first (Xero, QuickBooks, Zoho Books).
5. E-signature provider, if any.
6. Default retention periods.
7. Whether to support local models.
8. WhatsApp channel, yes or later.
9. Hosted demo: where and when.
10. Any change to eval thresholds.
