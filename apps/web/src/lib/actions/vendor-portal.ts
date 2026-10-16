// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
"use server";

/**
 * Server actions of the vendor portal (brief 4.7). Each one checks its input shape and
 * calls the portal API with the contact's portal session from its HTTP-only cookie
 * (`../vendor-portal/session`). The API decides everything; the browser never sees the
 * token.
 */
import { z } from "zod";

import type { ActionResult } from "../action-result";
import { clearVendorPortalToken, setVendorPortalToken, vendorPortalCall } from "../vendor-portal/session";
import {
  isVendorLoginToken,
  ORG_RE,
  type VendorPortalDetails,
  type VendorPortalInvoice,
  type VendorPortalInvoiceDetail,
  type VendorPortalRequest,
} from "../vendor-portal/types";

const uuid = z.uuid();
const bad = { ok: false as const, code: "invalid_input" };

function fail(result: { code: string }): { ok: false; code: string } {
  return { ok: false, code: result.code };
}

/** Ask for a magic link. The answer is the same whether or not the address has access. */
export async function requestVendorPortalLink(input: { org: string; email: string; locale: "en" | "ar" }): Promise<ActionResult> {
  const parsed = z
    .object({ org: z.string().regex(ORG_RE), email: z.email().max(320), locale: z.enum(["en", "ar"]) })
    .safeParse(input);
  if (!parsed.success) return bad;
  const result = await vendorPortalCall("/v1/pay/vendor-portal/login-link", {
    method: "POST",
    body: parsed.data,
    session: false,
  });
  return result.ok ? { ok: true } : fail(result);
}

/** Use a magic link once: the portal session goes into its HTTP-only cookie. */
export async function openVendorPortalSession(token: string): Promise<ActionResult<{ locale: string }>> {
  if (!isVendorLoginToken(token)) return { ok: false, code: "not_found" };
  const result = await vendorPortalCall<{ session_token: string; expires_at: string; locale: string }>(
    "/v1/pay/vendor-portal/session",
    { method: "POST", body: { token }, session: false },
  );
  if (!result.ok) return fail(result);
  await setVendorPortalToken(result.data.session_token, result.data.expires_at);
  return { ok: true, locale: result.data.locale };
}

export async function signOutOfVendorPortal(): Promise<ActionResult> {
  await vendorPortalCall("/v1/pay/vendor-portal/sign-out", { method: "POST" });
  await clearVendorPortalToken();
  return { ok: true };
}

export async function loadVendorInvoices(): Promise<ActionResult<{ invoices: VendorPortalInvoice[] }>> {
  const result = await vendorPortalCall<VendorPortalInvoice[]>("/v1/pay/vendor-portal/invoices");
  return result.ok ? { ok: true, invoices: result.data } : fail(result);
}

export async function writeToAccountsPayable(
  invoiceId: string,
  text: string,
): Promise<ActionResult<{ invoice: VendorPortalInvoiceDetail }>> {
  const parsed = z.object({ id: uuid, text: z.string().trim().min(1).max(4000) }).safeParse({ id: invoiceId, text });
  if (!parsed.success) return bad;
  const result = await vendorPortalCall<VendorPortalInvoiceDetail>(`/v1/pay/vendor-portal/invoices/${parsed.data.id}/messages`, {
    method: "POST",
    body: { text: parsed.data.text },
  });
  return result.ok ? { ok: true, invoice: result.data } : fail(result);
}

/** Ask a person to review a decision about one of the vendor's own invoices (an appeal). */
export async function askForHumanReview(
  invoiceId: string,
  decisionId: string,
  reason: string,
): Promise<ActionResult<{ invoice: VendorPortalInvoiceDetail }>> {
  const parsed = z
    .object({ id: uuid, decisionId: z.string().regex(/^dec_[0-9A-Z]{26}$/), reason: z.string().trim().min(1).max(4000) })
    .safeParse({ id: invoiceId, decisionId, reason });
  if (!parsed.success) return bad;
  const result = await vendorPortalCall<VendorPortalInvoiceDetail>(`/v1/pay/vendor-portal/invoices/${parsed.data.id}/appeals`, {
    method: "POST",
    body: { decision_id: parsed.data.decisionId, reason: parsed.data.reason },
  });
  return result.ok ? { ok: true, invoice: result.data } : fail(result);
}

const contactSchema = z.object({
  name: z.string().trim().min(1).max(120),
  role: z.string().trim().max(80).nullable(),
  email: z.string().trim().max(254).nullable(),
  phone: z.string().trim().max(40).nullable(),
});

export async function updateVendorContacts(input: {
  contacts: Array<z.infer<typeof contactSchema>>;
}): Promise<ActionResult<{ details: VendorPortalDetails }>> {
  const parsed = z
    .object({ contacts: z.array(contactSchema).max(20) })
    .safeParse(input);
  if (!parsed.success) return bad;
  const contacts = parsed.data.contacts.map((c) => ({
    name: c.name,
    role: c.role || null,
    email: c.email || null,
    phone: c.phone || null,
  }));
  // The phone number on file is accounts payable's to change, never the portal's.
  const body: Record<string, unknown> = { contacts };
  const result = await vendorPortalCall<VendorPortalDetails>("/v1/pay/vendor-portal/details/contacts", { method: "PUT", body });
  return result.ok ? { ok: true, details: result.data } : fail(result);
}

export async function requestBankDetailChange(input: {
  iban: string;
  accountName: string;
  bankName: string;
  swift: string;
  note: string;
}): Promise<ActionResult<{ request: VendorPortalRequest }>> {
  const parsed = z
    .object({
      iban: z.string().trim().min(5).max(64),
      accountName: z.string().trim().min(2).max(200),
      bankName: z.string().trim().max(160),
      swift: z.string().trim().max(16),
      note: z.string().trim().max(1000),
    })
    .safeParse(input);
  if (!parsed.success) return bad;
  const d = parsed.data;
  const result = await vendorPortalCall<{ request: VendorPortalRequest }>("/v1/pay/vendor-portal/bank-change", {
    method: "POST",
    body: {
      iban: d.iban,
      account_name: d.accountName,
      bank_name: d.bankName || null,
      swift: d.swift || null,
      note: d.note || null,
    },
  });
  return result.ok ? { ok: true, request: result.data.request } : fail(result);
}
