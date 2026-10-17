// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
"use server";

/**
 * Server actions of the vendor's onboarding form (WP4.2, brief 4.1.1). The browser never
 * sees the onboarding session: it lives in an HTTP-only cookie (`../pay/vendor-session`).
 * The API validates every field (IBAN checksum, TRN, phone) and decides everything.
 */
import { z } from "zod";

import type { ActionResult } from "../action-result";
import type { PayOnboardingPage } from "../pay/types";
import { clearVendorToken, publicPayCall, setVendorToken } from "../pay/vendor-session";

const LINK_RE = /^vi1\.[0-9a-f]{32}\.[0-9a-f]{32}\.[A-Za-z0-9_-]{43}$/;
const bad = { ok: false as const, code: "invalid_input" };
const text = (max: number) => z.string().trim().max(max);

const formInput = z.object({
  legal_name: z.string().trim().min(2).max(200),
  legal_name_ar: text(200),
  trade_name: text(200),
  country: z.string().regex(/^[A-Z]{2}$/),
  trade_licence: text(64),
  trn: text(40),
  address: z.object({ line1: text(200), line2: text(200), city: text(200), region: text(200), po_box: text(20) }),
  contact: z.object({ name: z.string().trim().min(1).max(120), role: text(80), email: text(254), phone: text(40) }),
  phone_on_file: z.string().trim().min(5).max(40),
  email_domain: text(253),
  bank: z.object({
    iban: z.string().trim().min(5).max(64),
    account_name: z.string().trim().min(2).max(200),
    bank_name: text(160),
    swift: text(16),
  }),
});

export type VendorOnboardingForm = z.infer<typeof formInput>;

function present(record: Record<string, string>): Record<string, string> {
  return Object.fromEntries(Object.entries(record).filter(([, v]) => v !== ""));
}

/** Use the signed link once: the session goes into its HTTP-only cookie. */
export async function openVendorLink(token: string): Promise<ActionResult> {
  if (!LINK_RE.test(token)) return { ok: false, code: "not_found" };
  const result = await publicPayCall<{ session_token: string; expires_at: string }>(
    "/v1/pay/public/vendor-onboarding/open",
    { method: "POST", body: { token } },
  );
  if (!result.ok) return { ok: false, code: result.code };
  await setVendorToken(result.data.session_token, result.data.expires_at);
  return { ok: true };
}

export async function loadVendorForm(): Promise<ActionResult<{ page: PayOnboardingPage }>> {
  const result = await publicPayCall<PayOnboardingPage>("/v1/pay/vendor-onboarding/me", { session: true });
  return result.ok ? { ok: true, page: result.data } : { ok: false, code: result.code };
}

/** Submit the form. The session ends with it. */
export async function submitVendorForm(form: VendorOnboardingForm): Promise<ActionResult<{ reference: string }>> {
  const parsed = formInput.safeParse(form);
  if (!parsed.success) return bad;
  const f = parsed.data;
  const body = {
    ...present({
      legal_name: f.legal_name,
      legal_name_ar: f.legal_name_ar,
      trade_name: f.trade_name,
      country: f.country,
      trade_licence: f.trade_licence,
      trn: f.trn,
      phone_on_file: f.phone_on_file,
    }),
    address: present(f.address),
    contacts: [present(f.contact)],
    email_domains: f.email_domain ? [f.email_domain.toLowerCase()] : [],
    bank: present(f.bank),
    confirm: true,
  };
  const result = await publicPayCall<{ reference: string; status: string }>("/v1/pay/vendor-onboarding/me/submit", {
    method: "POST",
    body,
    session: true,
  });
  if (!result.ok) return { ok: false, code: result.code };
  await clearVendorToken();
  return { ok: true, reference: result.data.reference };
}
