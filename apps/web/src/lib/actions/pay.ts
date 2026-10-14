// SPDX-License-Identifier: AGPL-3.0-only
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
import type { PayInviteCreated, PayInvite, PayTicketDetail, PayVendorCheck, PayVendorDetail } from "../pay/types";

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

const vendorInput = vendorFields.extend({ bank: bankInput.optional() });

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
