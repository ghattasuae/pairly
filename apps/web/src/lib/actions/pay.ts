// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
"use server";

/**
 * Server actions for Pay vendors and bank-detail changes (WP4.2, WP4.3). Each one checks
 * its input shape and calls the API with the signed-in user's token. The API decides
 * everything: permissions, the second factor for verifying and approving, segregation
 * of duties (the requester never verifies, neither approves), the payment hold and the
 * number to call (always the one on file).
 */
import { z } from "zod";

import type { ActionResult } from "../action-result";
import { callApi, toAction } from "../api/call";
import type {
  PayCreditApplied,
  PayInviteCreated,
  PayInvite,
  PayInvoiceCoding,
  PayInvoiceDetail,
  PayMessage,
  PayMessageDraft,
  PayModelLine,
  PayPoDetail,
  PayPortalAccess,
  PayRunDetail,
  PayRunPreview,
  PaySettings,
  PayThreadMessage,
  PayTicketDetail,
  PayVendorCheck,
  PayVendorDetail,
} from "../pay/types";

const uuid = z.uuid();
const bad = { ok: false as const, code: "invalid_input" };
const optional = (max: number) => z.string().trim().max(max).optional();
const reason = z.string().trim().min(1).max(2000);

const bankInput = z.object({
  iban: z.string().trim().min(5).max(64),
  account_name: z.string().trim().min(2).max(200),
  bank_name: optional(160),
  swift: optional(16),
});

const contactInput = z.object({
  name: z.string().trim().min(1).max(120),
  role: optional(80),
  email: optional(254),
  phone: optional(40),
});

const vendorFields = z.object({
  legal_name: z.string().trim().min(2).max(200),
  legal_name_ar: optional(200),
  trade_name: optional(200),
  country: z.string().regex(/^[A-Z]{2}$/),
  trade_licence: optional(64),
  trn: optional(40),
  address: z
    .object({
      line1: optional(200),
      line2: optional(200),
      city: optional(200),
      region: optional(200),
      po_box: optional(20),
    })
    .default({}),
  contacts: z.array(contactInput).max(10).default([]),
  phone_on_file: optional(40),
  email_domains: z.array(z.string().trim().toLowerCase().max(253)).max(10).default([]),
  category: optional(64),
});

const vendorInput = vendorFields.extend({
  bank: bankInput.optional(),
  // Where the number on file comes from: required with bank details (security review
  // 2026-10, M-03), which then wait in a change ticket.
  phone_source: z
    .object({
      source: z.enum(["trade_licence", "signed_contract", "prior_correspondence", "official_registry"]),
      reference: z.string().trim().min(3).max(500),
    })
    .optional(),
});

export type PayBankInput = z.input<typeof bankInput>;
export type PayVendorInput = z.input<typeof vendorInput>;

type Defined<T> = { [K in keyof T]: Exclude<T[K], undefined> };

/** Drop empty strings and undefined: the API reads a missing field as "not given". */
function compact<T>(value: T): Defined<T> {
  if (Array.isArray(value)) return value.map(compact) as Defined<T>;
  if (value && typeof value === "object") {
    return Object.fromEntries(
      Object.entries(value as Record<string, unknown>)
        .filter(([, v]) => v !== "" && v !== undefined)
        .map(([k, v]) => [k, compact(v)]),
    ) as Defined<T>;
  }
  return value as Defined<T>;
}

export async function checkVendor(input: {
  legal_name: string;
  trade_name?: string;
  trn?: string;
  iban?: string;
  swift?: string;
}): Promise<ActionResult<{ check: PayVendorCheck }>> {
  const parsed = z
    .object({ legal_name: z.string().max(200), trade_name: optional(200), trn: optional(40), iban: optional(64), swift: optional(16) })
    .safeParse(input);
  if (!parsed.success) return bad;
  const result = await callApi((api) => api.POST("/v1/pay/vendors/check", { body: compact(parsed.data) }));
  return toAction(result, (check) => ({ check }));
}

export async function createVendor(
  input: PayVendorInput,
  acknowledgeSimilar: boolean,
): Promise<ActionResult<{ vendor: PayVendorDetail }>> {
  const parsed = vendorInput.safeParse(input);
  if (!parsed.success) return bad;
  const result = await callApi((api) =>
    api.POST("/v1/pay/vendors", {
      body: { ...compact(parsed.data), acknowledge_similar: acknowledgeSimilar } as never,
    }),
  );
  return toAction(result, (vendor) => ({ vendor }));
}

export async function requestBankChange(
  vendorId: string,
  input: {
    bank: PayBankInput;
    source: "staff" | "email" | "invoice" | "vendor_portal";
    note?: string;
    request_contact?: { name?: string; phone?: string; email?: string };
  },
): Promise<ActionResult<{ ticket: PayTicketDetail }>> {
  const parsed = z
    .object({
      bank: bankInput,
      source: z.enum(["staff", "email", "invoice", "vendor_portal"]),
      note: optional(2000),
      request_contact: z.object({ name: optional(120), phone: optional(40), email: optional(254) }).optional(),
    })
    .safeParse(input);
  if (!uuid.safeParse(vendorId).success || !parsed.success) return bad;
  const result = await callApi(
    (api) =>
      api.POST("/v1/pay/vendors/{vendor_id}/bank-changes", {
        params: { path: { vendor_id: vendorId } },
        body: compact(parsed.data) as never,
      }),
    { okStatuses: [201] },
  );
  return toAction(result, (ticket) => ({ ticket }));
}

export async function recordCall(
  ticketId: string,
  input: { result: "confirmed" | "not_confirmed" | "no_answer"; notes?: string; called_number_on_file: boolean },
): Promise<ActionResult<{ ticket: PayTicketDetail }>> {
  const parsed = z
    .object({
      result: z.enum(["confirmed", "not_confirmed", "no_answer"]),
      notes: optional(2000),
      called_number_on_file: z.boolean(),
    })
    .strict()
    .safeParse(input);
  if (!uuid.safeParse(ticketId).success || !parsed.success) return bad;
  const result = await callApi((api) =>
    api.POST("/v1/pay/bank-changes/{ticket_id}/calls", {
      params: { path: { ticket_id: ticketId } },
      body: compact(parsed.data),
    }),
  );
  return toAction(result, (ticket) => ({ ticket }));
}

export async function startApproval(ticketId: string): Promise<ActionResult<{ ticket: PayTicketDetail }>> {
  if (!uuid.safeParse(ticketId).success) return bad;
  const result = await callApi((api) =>
    api.POST("/v1/pay/bank-changes/{ticket_id}/start-approval", { params: { path: { ticket_id: ticketId } } }),
  );
  return toAction(result, (ticket) => ({ ticket }));
}

export async function cancelBankChange(
  ticketId: string,
  why: string,
  keepHold: boolean,
): Promise<ActionResult<{ ticket: PayTicketDetail }>> {
  if (!uuid.safeParse(ticketId).success || !reason.safeParse(why).success) return bad;
  const result = await callApi((api) =>
    api.POST("/v1/pay/bank-changes/{ticket_id}/cancel", {
      params: { path: { ticket_id: ticketId } },
      body: { reason: why.trim(), keep_hold: keepHold },
    }),
  );
  return toAction(result, (ticket) => ({ ticket }));
}

const callbackSource = z.enum(["trade_licence", "signed_contract", "prior_correspondence", "official_registry"]);
const callbackSchema = z
  .object({
    phone: z.string().trim().min(5).max(40),
    source: callbackSource,
    reference: z.string().trim().min(3).max(500),
  })
  .strict();

/** Accept an onboarding. With bank details it needs the call-back number from an
 * independent source (security review 2026-10, M-05): the number the vendor's own form
 * gave is never the one called. */
export async function acceptOnboarding(
  vendorId: string,
  input: { note: string; callback?: { phone: string; source: string; reference: string } },
): Promise<ActionResult<{ vendor: PayVendorDetail }>> {
  const parsed = z.object({ note: z.string().max(2000), callback: callbackSchema.optional() }).strict().safeParse(input);
  if (!uuid.safeParse(vendorId).success || !parsed.success) return bad;
  const note = parsed.data.note.trim();
  const result = await callApi((api) =>
    api.POST("/v1/pay/vendors/{vendor_id}/accept", {
      params: { path: { vendor_id: vendorId } },
      body: { ...(note ? { note } : {}), ...(parsed.data.callback ? { callback: parsed.data.callback } : {}) },
    }),
  );
  return toAction(result, (vendor) => ({ vendor }));
}

/** Record the call-back number from an independent source on an onboarding change that
 * has none yet (M-05). */
export async function recordCallbackNumber(
  ticketId: string,
  input: { phone: string; source: string; reference: string },
): Promise<ActionResult<{ ticket: PayTicketDetail }>> {
  const parsed = callbackSchema.safeParse(input);
  if (!uuid.safeParse(ticketId).success || !parsed.success) return bad;
  const result = await callApi((api) =>
    api.POST("/v1/pay/bank-changes/{ticket_id}/callback-number", {
      params: { path: { ticket_id: ticketId } },
      body: parsed.data,
    }),
  );
  return toAction(result, (ticket) => ({ ticket }));
}

type VendorAction = "accept" | "reject" | "hold" | "release";

/** Accept or reject an onboarding, or place or release a payment hold. */
export async function vendorAction(
  vendorId: string,
  action: VendorAction,
  text: string,
): Promise<ActionResult<{ vendor: PayVendorDetail }>> {
  if (!uuid.safeParse(vendorId).success || !["accept", "reject", "hold", "release"].includes(action)) return bad;
  const path = { params: { path: { vendor_id: vendorId } } };
  if (action === "accept") {
    if (text.length > 2000) return bad;
    const result = await callApi((api) =>
      api.POST("/v1/pay/vendors/{vendor_id}/accept", { ...path, body: text.trim() ? { note: text.trim() } : {} }),
    );
    return toAction(result, (vendor) => ({ vendor }));
  }
  if (!reason.safeParse(text).success) return bad;
  const body = { reason: text.trim() };
  const result = await callApi((api) =>
    action === "reject"
      ? api.POST("/v1/pay/vendors/{vendor_id}/reject", { ...path, body })
      : action === "hold"
        ? api.POST("/v1/pay/vendors/{vendor_id}/hold", { ...path, body })
        : api.POST("/v1/pay/vendors/{vendor_id}/hold/release", { ...path, body }),
  );
  return toAction(result, (vendor) => ({ vendor }));
}

const paymentTerms = z.string().trim().max(120);

/** Set or clear a vendor's agreed payment terms ("2/10 net 30"): the API audits it and
 * the early-payment optimiser reads them. An empty value clears the terms. */
export async function updatePaymentTerms(
  vendorId: string,
  terms: string,
): Promise<ActionResult<{ vendor: PayVendorDetail }>> {
  const parsed = paymentTerms.safeParse(terms);
  if (!uuid.safeParse(vendorId).success || !parsed.success) return bad;
  const result = await callApi((api) =>
    api.PATCH("/v1/pay/vendors/{vendor_id}", {
      params: { path: { vendor_id: vendorId } },
      body: { payment_terms: parsed.data || null },
    }),
  );
  return toAction(result, (vendor) => ({ vendor }));
}

export async function createInvite(input: {
  company_hint?: string;
  email?: string;
  locale: "en" | "ar";
}): Promise<ActionResult<{ invite: PayInviteCreated }>> {
  const parsed = z
    .object({ company_hint: optional(200), email: z.union([z.literal(""), z.email().max(254)]).optional(), locale: z.enum(["en", "ar"]) })
    .safeParse(input);
  if (!parsed.success) return bad;
  const result = await callApi((api) => api.POST("/v1/pay/vendor-invites", { body: compact(parsed.data) }), {
    okStatuses: [201],
  });
  return toAction(result, (invite) => ({ invite }));
}

export async function revokeInvite(id: string): Promise<ActionResult<{ invite: PayInvite }>> {
  if (!uuid.safeParse(id).success) return bad;
  const result = await callApi((api) =>
    api.POST("/v1/pay/vendor-invites/{invite_id}/revoke", { params: { path: { invite_id: id } } }),
  );
  return toAction(result, (invite) => ({ invite }));
}

// ------------------------------------------------------------------ invoices (WP4.5)

const fieldPath = z.string().regex(/^[a-z_]+(\[\d{1,3}\])?(\.[a-z_]+)?$/).max(200);

/** Correct one field of an invoice, with a reason. The API files it under the vendor. */
export async function correctInvoiceField(
  invoiceId: string,
  input: { field_path: string; new_value: string | null; reason: string },
): Promise<ActionResult<{ invoice: PayInvoiceDetail }>> {
  const parsed = z
    .object({ field_path: fieldPath, new_value: z.string().trim().max(2000).nullable(), reason })
    .strict()
    .safeParse(input);
  if (!uuid.safeParse(invoiceId).success || !parsed.success) return bad;
  const body = {
    field_path: parsed.data.field_path,
    new_value: parsed.data.new_value === "" ? null : parsed.data.new_value,
    reason: parsed.data.reason,
  };
  const result = await callApi((api) =>
    api.POST("/v1/pay/invoices/{invoice_id}/corrections", { params: { path: { invoice_id: invoiceId } }, body }),
  );
  return toAction(result, (invoice) => ({ invoice }));
}

/**
 * Mark reviewed (to ready), reject (with a reason), link the vendor, run the checks
 * again, or send for approval (with an optional note).
 */
export async function invoiceAction(
  invoiceId: string,
  action: "review" | "reject" | "vendor" | "checks" | "submit",
  text: string,
): Promise<ActionResult<{ invoice: PayInvoiceDetail }>> {
  if (!uuid.safeParse(invoiceId).success) return bad;
  const path = { params: { path: { invoice_id: invoiceId } } };
  if (action === "checks") {
    const result = await callApi((api) => api.POST("/v1/pay/invoices/{invoice_id}/checks", path));
    return toAction(result, (invoice) => ({ invoice }));
  }
  if (action === "submit") {
    if (text.length > 1000) return bad;
    const result = await callApi((api) =>
      api.POST("/v1/pay/invoices/{invoice_id}/submit", { ...path, body: text.trim() ? { note: text.trim() } : {} }),
    );
    return toAction(result, (invoice) => ({ invoice }));
  }
  if (action === "review") {
    if (text.length > 1000) return bad;
    const result = await callApi((api) =>
      api.POST("/v1/pay/invoices/{invoice_id}/review", { ...path, body: text.trim() ? { note: text.trim() } : {} }),
    );
    return toAction(result, (invoice) => ({ invoice }));
  }
  if (action === "reject") {
    if (!reason.safeParse(text).success || text.trim().length > 1000) return bad;
    const result = await callApi((api) =>
      api.POST("/v1/pay/invoices/{invoice_id}/reject", { ...path, body: { reason: text.trim() } }),
    );
    return toAction(result, (invoice) => ({ invoice }));
  }
  if (action === "vendor") {
    if (!uuid.safeParse(text).success) return bad;
    const result = await callApi((api) =>
      api.POST("/v1/pay/invoices/{invoice_id}/vendor", { ...path, body: { vendor_id: text } }),
    );
    return toAction(result, (invoice) => ({ invoice }));
  }
  return bad;
}

const codingKey = z.union([z.literal(""), z.string().regex(/^[a-z][a-z0-9_]{0,63}$/)]);

/** Set the department and category the approval rules route on. The checks run again. */
export async function codeInvoice(
  invoiceId: string,
  input: { department: string; category: string },
): Promise<ActionResult<{ invoice: PayInvoiceDetail }>> {
  const parsed = z.object({ department: codingKey, category: codingKey }).strict().safeParse(input);
  if (!uuid.safeParse(invoiceId).success || !parsed.success) return bad;
  const result = await callApi((api) =>
    api.POST("/v1/pay/invoices/{invoice_id}/coding", {
      params: { path: { invoice_id: invoiceId } },
      body: { department: parsed.data.department || null, category: parsed.data.category || null },
    }),
  );
  return toAction(result, (invoice) => ({ invoice }));
}

// ------------------------------------------------------------------ GL coding (brief 4.5.3)

const glCode = z.union([z.null(), z.string().regex(/^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$/)]);
const lineCoding = z
  .object({ line_index: z.number().int().min(0).max(999), gl_code: glCode, cost_centre: glCode })
  .strict();

/** Code invoice lines (GL account and cost centre). The checks run again. */
export async function codeInvoiceLines(
  invoiceId: string,
  lines: { line_index: number; gl_code: string | null; cost_centre: string | null }[],
): Promise<ActionResult<{ coding: PayInvoiceCoding }>> {
  const parsed = z.array(lineCoding).min(1).max(500).safeParse(lines);
  if (!uuid.safeParse(invoiceId).success || !parsed.success) return bad;
  const result = await callApi((api) =>
    api.PUT("/v1/pay/invoices/{invoice_id}/coding/lines", {
      params: { path: { invoice_id: invoiceId } },
      body: { lines: parsed.data },
    }),
  );
  return toAction(result, (coding) => ({ coding }));
}

/** Ask the model for lines the history cannot code. Shown only; nothing is applied. */
export async function askModelCoding(
  invoiceId: string,
  lineIndexes: number[],
): Promise<ActionResult<{ lines: PayModelLine[] }>> {
  const parsed = z.array(z.number().int().min(0).max(999)).max(50).safeParse(lineIndexes);
  if (!uuid.safeParse(invoiceId).success || !parsed.success) return bad;
  const result = await callApi((api) =>
    api.POST("/v1/pay/invoices/{invoice_id}/coding/model-suggestions", {
      params: { path: { invoice_id: invoiceId } },
      body: { line_indexes: parsed.data },
    }),
  );
  return toAction(result, (data) => ({ lines: data.lines }));
}

// ------------------------------------------------------------------ exception messages (brief 4.5.4)

const category = z.enum([
  "no_po",
  "price_variance",
  "quantity_variance",
  "missing_receipt",
  "duplicate_suspect",
  "bank_mismatch",
  "policy_breach",
  "low_confidence",
  "other",
]);
const templateId = z.string().regex(/^[a-z_]{1,32}$/);
const locale = z.enum(["en", "ar"]);

/** A drafted message from a template, with an optional personal paragraph (nothing saved). */
export async function draftExceptionMessage(
  invoiceId: string,
  input: { category: string; template_id: string; locale: "en" | "ar"; personalise: boolean },
): Promise<ActionResult<{ draft: PayMessageDraft }>> {
  const parsed = z
    .object({ category, template_id: templateId, locale, personalise: z.boolean() })
    .strict()
    .safeParse(input);
  if (!uuid.safeParse(invoiceId).success || !parsed.success) return bad;
  const result = await callApi((api) =>
    api.POST("/v1/pay/invoices/{invoice_id}/messages/draft", {
      params: { path: { invoice_id: invoiceId } },
      body: parsed.data,
    }),
  );
  return toAction(result, (draft) => ({ draft }));
}

/** Save a message for approval. It is sent only after another person approves the exact text. */
export async function sendExceptionMessage(
  invoiceId: string,
  input: {
    category: string;
    template_id: string;
    locale: "en" | "ar";
    message: string;
    recipient_email: string;
    ai_note: boolean;
    model_call_id: string | null;
  },
): Promise<ActionResult<{ message: PayMessage }>> {
  const parsed = z
    .object({
      category,
      template_id: templateId,
      locale,
      message: z.string().trim().min(1).max(3000),
      recipient_email: z.string().trim().min(3).max(254),
      ai_note: z.boolean(),
      model_call_id: z.union([z.null(), uuid]),
    })
    .strict()
    .safeParse(input);
  if (!uuid.safeParse(invoiceId).success || !parsed.success) return bad;
  const result = await callApi(
    (api) =>
      api.POST("/v1/pay/invoices/{invoice_id}/messages", {
        params: { path: { invoice_id: invoiceId } },
        body: parsed.data,
      }),
    { okStatuses: [201] },
  );
  return toAction(result, (message) => ({ message }));
}

// ------------------------------------------------------------------ payment runs (WP4.7)

const decimal = z.string().trim().regex(/^\d{1,13}(\.\d{1,4})?$/);
const isoDate = z.string().regex(/^\d{4}-\d{2}-\d{2}$/);

const proposalInput = z
  .object({
    currency: z.string().regex(/^[A-Z]{3}$/),
    cash_limit: decimal,
    execution_date: isoDate,
    due_on_or_before: z.union([z.literal(""), isoDate]),
    include_early_payment: z.boolean().optional(),
  })
  .strict();

export type PayRunProposalInput = z.input<typeof proposalInput>;

function proposalBody(input: z.output<typeof proposalInput>) {
  return {
    currency: input.currency,
    cash_limit: input.cash_limit,
    execution_date: input.execution_date,
    ...(input.due_on_or_before ? { due_on_or_before: input.due_on_or_before } : {}),
    ...(input.include_early_payment ? { include_early_payment: true } : {}),
  };
}

/** What a proposal would pay and leave out (writes nothing). */
export async function previewRun(input: PayRunProposalInput): Promise<ActionResult<{ preview: PayRunPreview }>> {
  const parsed = proposalInput.safeParse(input);
  if (!parsed.success) return bad;
  const result = await callApi((api) =>
    api.POST("/v1/pay/payment-runs/preview", { body: proposalBody(parsed.data) as never }),
  );
  return toAction(result, (preview) => ({ preview }));
}

/** Propose the run; a finance controller approves it in the inbox. */
export async function proposeRun(input: PayRunProposalInput): Promise<ActionResult<{ run: PayRunDetail }>> {
  const parsed = proposalInput.safeParse(input);
  if (!parsed.success) return bad;
  const result = await callApi(
    (api) => api.POST("/v1/pay/payment-runs", { body: proposalBody(parsed.data) as never }),
    { okStatuses: [201] },
  );
  return toAction(result, (run) => ({ run }));
}

/** Mark the run's unpaid invoices paid (after the bank confirmed), or cancel the run. */
export async function runAction(
  runId: string,
  action: "paid" | "cancel",
  text: string,
): Promise<ActionResult<{ run: PayRunDetail }>> {
  if (!uuid.safeParse(runId).success) return bad;
  const path = { params: { path: { run_id: runId } } };
  if (action === "paid") {
    if (!/^[\w./:#-]{1,64}$/.test(text.trim())) return bad;
    const result = await callApi((api) =>
      api.POST("/v1/pay/payment-runs/{run_id}/paid", { ...path, body: { reference: text.trim() } }),
    );
    return toAction(result, (run) => ({ run }));
  }
  if (!reason.safeParse(text).success || text.trim().length > 1000) return bad;
  const result = await callApi((api) =>
    api.POST("/v1/pay/payment-runs/{run_id}/cancel", { ...path, body: { reason: text.trim() } }),
  );
  return toAction(result, (run) => ({ run }));
}

// ------------------------------------------------------------------ purchase orders

/** One problem of a CSV import: its row (1 is the header, 0 the whole file). */
export interface PayImportProblem {
  row: number;
  field: string | null;
  code: string;
}

export type PayImportResult =
  | { ok: true; created: number; numbers: string[] }
  | { ok: false; code: string; problems: PayImportProblem[] };

function importProblems(error: unknown): PayImportProblem[] {
  const list = (error as { errors?: unknown } | null)?.errors;
  if (!Array.isArray(list)) return [];
  return list
    .filter((e): e is Record<string, unknown> => !!e && typeof e === "object")
    .map((e) => ({
      row: typeof e.row === "number" ? e.row : 0,
      field: typeof e.field === "string" ? e.field : null,
      code: typeof e.code === "string" ? e.code : "invalid",
    }))
    .slice(0, 50);
}

/**
 * Import purchase orders or goods receipts from a CSV (one row per line). The API
 * validates everything and creates nothing when any row has a problem; the problems
 * come back with their rows.
 */
export async function importPurchaseCsv(kind: "orders" | "receipts", csv: string): Promise<PayImportResult> {
  if ((kind !== "orders" && kind !== "receipts") || typeof csv !== "string") return { ...bad, problems: [] };
  if (csv.trim().length === 0 || csv.length > 1_000_000) return { ...bad, problems: [] };
  let error: unknown = null;
  const result = await callApi(async (api) => {
    const answer =
      kind === "orders"
        ? await api.POST("/v1/pay/purchase-orders/import", { body: { csv } })
        : await api.POST("/v1/pay/goods-receipts/import", { body: { csv } });
    error = answer.error;
    return answer;
  });
  if (result.ok) return { ok: true, created: result.data.created, numbers: result.data.numbers };
  return { ok: false, code: result.code, problems: importProblems(error) };
}

export type PayStatementResult =
  | { ok: true; reference: string; lines: number; matched: number; unmatched: number }
  | { ok: false; code: string; problems: PayImportProblem[] };

/**
 * Import a bank statement (CSV or camt.053) and match it with payments. The API checks
 * every line and imports nothing when one has a problem; the problems come back with
 * their rows. The same file twice is refused.
 */
export async function importStatement(format: "csv" | "camt053", content: string): Promise<PayStatementResult> {
  if ((format !== "csv" && format !== "camt053") || typeof content !== "string") return { ...bad, problems: [] };
  if (content.trim().length === 0 || content.length > 1_000_000) return { ...bad, problems: [] };
  let error: unknown = null;
  const result = await callApi(async (api) => {
    const answer = await api.POST("/v1/pay/reconciliation/statements", { body: { format, content } });
    error = answer.error;
    return answer;
  });
  if (result.ok) {
    return {
      ok: true,
      reference: result.data.statement.reference,
      lines: result.data.statement.line_count,
      matched: result.data.matched,
      unmatched: result.data.unmatched,
    };
  }
  return { ok: false, code: result.code, problems: importProblems(error) };
}

/** Match the unmatched statement lines again now (the hourly job does the same). */
export async function matchStatementsNow(): Promise<ActionResult<{ matched: number; unmatched: number }>> {
  const result = await callApi((api) => api.POST("/v1/pay/reconciliation/match"));
  return toAction(result, (data) => ({ matched: data.matched, unmatched: data.unmatched }));
}

/**
 * Close a purchase order (it takes no more invoices) or cancel it (a second factor;
 * refused while an invoice of it is approved or paid). Both need a reason.
 */
export async function endPurchaseOrder(
  poId: string,
  action: "close" | "cancel",
  why: string,
): Promise<ActionResult<{ order: PayPoDetail }>> {
  if (!uuid.safeParse(poId).success || (action !== "close" && action !== "cancel")) return bad;
  if (!reason.safeParse(why).success || why.trim().length > 500) return bad;
  const path = { params: { path: { po_id: poId } } };
  const body = { reason: why.trim() };
  const result = await callApi((api) =>
    action === "close"
      ? api.POST("/v1/pay/purchase-orders/{po_id}/close", { ...path, body })
      : api.POST("/v1/pay/purchase-orders/{po_id}/cancel", { ...path, body }),
  );
  return toAction(result, (order) => ({ order }));
}

// ------------------------------------------------------------------ settings

const decimalText = (max: number) =>
  z
    .string()
    .trim()
    .regex(/^\d{1,13}(\.\d{1,4})?$/)
    .refine((v) => Number(v) <= max);

const settingsInput = z.object({
  cooling_off_hours: z.number().int().min(0).max(720),
  phone_change_window_days: z.number().int().min(0).max(365),
  checks: z.object({
    duplicate_window_days: z.number().int().min(0).max(366),
    match_price_pct: decimalText(100),
    match_quantity_pct: decimalText(100),
    match_total_pct: decimalText(100),
    match_total_amount: decimalText(1_000_000),
    require_po: z.boolean(),
    auto_code_high_confidence: z.boolean(),
    exception_sla_hours: z.number().int().min(1).max(720),
  }),
  payments: z.object({
    auto_approve_low_risk: z.boolean(),
    auto_approve_limit: decimalText(1_000_000_000),
    auto_approve_currency: z.string().regex(/^[A-Z]{3}$/),
  }),
});

export type PaySettingsInput = z.input<typeof settingsInput>;

/**
 * Save the Pay settings (`pay.settings.manage`, a second factor). Only the settings this
 * page shows are sent; the API keeps every other check and payment setting as it is.
 */
export async function savePaySettings(input: PaySettingsInput): Promise<ActionResult<{ settings: PaySettings }>> {
  const parsed = settingsInput.safeParse(input);
  if (!parsed.success) return bad;
  const result = await callApi((api) => api.PUT("/v1/pay/settings", { body: parsed.data as never }));
  return toAction(result, (settings) => ({ settings }));
}

// ------------------------------------------------------------------ vendor portal (brief 4.7)

/** Give a contact on file access to the vendor portal; the API emails them a sign-in link. */
export async function givePortalAccess(
  vendorId: string,
  input: { email: string; locale: "en" | "ar" },
): Promise<ActionResult<{ access: PayPortalAccess }>> {
  const parsed = z.object({ email: z.email().max(254), locale: z.enum(["en", "ar"]) }).safeParse(input);
  if (!uuid.safeParse(vendorId).success || !parsed.success) return bad;
  const result = await callApi(
    (api) =>
      api.POST("/v1/pay/vendors/{vendor_id}/portal-contacts", {
        params: { path: { vendor_id: vendorId } },
        body: parsed.data,
      }),
    { okStatuses: [201] },
  );
  return toAction(result, (access) => ({ access }));
}

export async function revokePortalAccess(vendorId: string, contactId: string): Promise<ActionResult<{ access: PayPortalAccess }>> {
  if (!uuid.safeParse(vendorId).success || !uuid.safeParse(contactId).success) return bad;
  const result = await callApi((api) =>
    api.POST("/v1/pay/vendors/{vendor_id}/portal-contacts/{contact_id}/revoke", {
      params: { path: { vendor_id: vendorId, contact_id: contactId } },
    }),
  );
  return toAction(result, (access) => ({ access }));
}

/** Answer the vendor in the portal thread of an invoice. */
export async function replyInPortal(invoiceId: string, text: string): Promise<ActionResult<{ thread: PayThreadMessage[] }>> {
  const parsed = z.string().trim().min(1).max(4000).safeParse(text);
  if (!uuid.safeParse(invoiceId).success || !parsed.success) return bad;
  const result = await callApi(
    (api) =>
      api.POST("/v1/pay/invoices/{invoice_id}/portal-replies", {
        params: { path: { invoice_id: invoiceId } },
        body: { text: parsed.data },
      }),
    { okStatuses: [201] },
  );
  return toAction(result, (thread) => ({ thread }));
}

/** Apply a credit note to an invoice of the same vendor (the vendor's choice by default). */
export async function applyCreditNote(
  creditId: string,
  input: { invoice_id: string | null; note: string },
): Promise<ActionResult<{ applied: PayCreditApplied }>> {
  const parsed = z.object({ invoice_id: uuid.nullable(), note: z.string().trim().max(1000) }).safeParse(input);
  if (!uuid.safeParse(creditId).success || !parsed.success) return bad;
  const result = await callApi((api) =>
    api.POST("/v1/pay/invoices/{invoice_id}/apply-credit", {
      params: { path: { invoice_id: creditId } },
      body: { invoice_id: parsed.data.invoice_id, note: parsed.data.note || null },
    }),
  );
  return toAction(result, (applied) => ({ applied }));
}
