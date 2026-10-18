// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type {
  VendorPortalDecision,
  VendorPortalDetails,
  VendorPortalInvoice,
  VendorPortalInvoiceDetail,
  VendorPortalMe,
} from "@/lib/vendor-portal/types";
import { ORG_RE, orgFromVendorToken, stepViews } from "@/lib/vendor-portal/types";

import { renderWithIntl } from "../../../test/intl";
import { navigationMock, routerSpies } from "../../../test/navigation-mock";

vi.mock("@/i18n/navigation", () => navigationMock);
vi.mock("next/navigation", () => ({ useRouter: () => routerSpies }));
vi.mock("@/lib/actions/vendor-portal", () => ({
  requestVendorPortalLink: vi.fn(),
  openVendorPortalSession: vi.fn(),
  signOutOfVendorPortal: vi.fn(),
  loadVendorInvoices: vi.fn(),
  writeToAccountsPayable: vi.fn(),
  askForHumanReview: vi.fn(),
  requestBankDetailChange: vi.fn(),
}));

const { VendorPortalSignIn } = await import("../vendor-portal/vendor-sign-in");
const { VendorPortalLogin } = await import("../vendor-portal/vendor-login");
const { VendorPortalHome } = await import("../vendor-portal/vendor-home");
const { VendorInvoiceDetail } = await import("../vendor-portal/vendor-invoice-detail");
const { VendorDetails } = await import("../vendor-portal/vendor-details");

const ORG = "0123456789abcdef0123456789abcdef";
const TOKEN = `vl1.${ORG}.${"1".repeat(32)}.${"2".repeat(32)}.${"3".repeat(32)}.${"A".repeat(43)}`;
const INVOICE_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";

const ME: VendorPortalMe = {
  organisation: "Acme Trading",
  org_id: ORG,
  vendor_name: "Gulf Office Supplies LLC",
  vendor_reference: "VEN-7K2QX9",
  contact_name: "Huda Rahman",
  email: "huda@gulf-office.example",
  locale: "en",
};

function invoice(patch: Partial<VendorPortalInvoice> = {}): VendorPortalInvoice {
  return {
    id: INVOICE_ID,
    reference: "AP-7Q2K9D",
    document_type: "invoice",
    invoice_number: "GOS-2026-0412",
    status: "scheduled",
    invoice_date: "2026-09-20",
    due_date: "2026-10-20",
    expected_payment_date: "2026-10-22",
    currency: "AED",
    total: "27138.43",
    credit_total: "0",
    amount_to_pay: "27138.43",
    received_at: "2026-09-21T08:00:00Z",
    steps: [
      { step: "received", at: "2026-09-21T08:00:00Z" },
      { step: "in_review", at: "2026-09-21T08:05:00Z" },
      { step: "approved", at: "2026-09-25T10:00:00Z" },
      { step: "scheduled", at: "2026-10-01T10:00:00Z" },
      { step: "paid", at: null },
    ],
    credit_for: null,
    payment_reference: null,
    can_message: true,
    messages: 1,
    ...patch,
  };
}

const DETAILS: VendorPortalDetails = {
  legal_name: "Gulf Office Supplies LLC",
  legal_name_ar: null,
  trade_name: null,
  reference: "VEN-7K2QX9",
  country: "AE",
  trn: "100123456700003",
  trn_status: "not_verified",
  address: { city: "Dubai" },
  contacts: [{ name: "Huda Rahman", role: "Accounts", email: "huda@gulf-office.example", phone: null }],
  phone_masked: "•••• 199",
  phone_changed_at: null,
  payment_hold: false,
  account: {
    iban_masked: "AE•• •••• •••• 1715",
    account_name: "Gulf Office Supplies LLC",
    bank_name: "Sahil Commercial Bank",
    swift: null,
    country: "AE",
    since: "2026-01-10T00:00:00Z",
  },
  open_requests: [],
};

describe("vendor portal helpers", () => {
  it("marks reached steps done, the last one current and the rest ahead", () => {
    expect(stepViews(invoice()).map((s) => s.state)).toEqual(["done", "done", "done", "current", "ahead"]);
    const rejected = stepViews(invoice({ status: "rejected", steps: [{ step: "received", at: "2026-09-21T08:00:00Z" }] }));
    expect(rejected.map((s) => s.step)).toEqual(["received", "rejected"]);
  });

  it("reads the organisation from a link, never from anything else", () => {
    expect(orgFromVendorToken(TOKEN)).toBe(ORG);
    expect(orgFromVendorToken(`pl1.${ORG}.x`)).toBeNull();
  });

  it("accepts the organisation's slug or its id in a shared link", () => {
    for (const org of [ORG, "acme-trading", "demo"]) expect(ORG_RE.test(org)).toBe(true);
    for (const org of ["", "a", "-acme", "Acme", "acme_trading", "acme trading", ORG.toUpperCase(), "a".repeat(64)]) {
      expect(ORG_RE.test(org)).toBe(false);
    }
  });
});

describe("vendor portal sign-in", () => {
  it("asks for a magic link and says the same thing whoever asks", async () => {
    const request = vi.fn().mockResolvedValue({ ok: true });
    renderWithIntl(<VendorPortalSignIn org={ORG} request={request} />);
    await userEvent.type(screen.getByLabelText("Email address"), "huda@gulf-office.example");
    await userEvent.click(screen.getByRole("button", { name: "Send me a link" }));
    expect(request).toHaveBeenCalledWith({ org: ORG, email: "huda@gulf-office.example", locale: "en" });
    expect(await screen.findByText(/If this address has access/)).toBeInTheDocument();
  });

  it("uses the link in the fragment once and offers a new one when it expired", async () => {
    window.history.replaceState(null, "", `/en/vendor-portal/login#${TOKEN}`);
    const open = vi.fn().mockResolvedValue({ ok: false, code: "not_found" });
    renderWithIntl(<VendorPortalLogin open={open} />);
    await waitFor(() => expect(open).toHaveBeenCalledTimes(1));
    expect(open).toHaveBeenCalledWith(TOKEN);
    expect(window.location.hash).toBe("");
    expect(await screen.findByText("This link has expired")).toBeInTheDocument();
  });
});

describe("vendor portal home", () => {
  it("shows each invoice with its status, dates and the next payment, in Arabic too", () => {
    renderWithIntl(<VendorPortalHome me={ME} invoices={[invoice()]} />, "ar");
    expect(screen.getAllByText("مجدولة للدفع").length).toBeGreaterThan(0);
    expect(document.querySelector("[data-expected-payment]")).not.toBeNull();
    expect(document.querySelector('[data-vendor-invoice][data-status="scheduled"]')).not.toBeNull();
  });

  it("sends a credit note only with the invoice it reduces", async () => {
    const submit = vi.fn().mockResolvedValue({
      ok: true,
      duplicate: false,
      invoice: invoice({ id: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb", reference: "AP-CREDIT1", document_type: "credit_note", status: "received" }),
    });
    renderWithIntl(<VendorPortalHome me={ME} invoices={[invoice({ status: "approved" })]} submit={submit} />);
    await userEvent.click(screen.getByLabelText("Credit note"));
    const file = new File(["%PDF-1.4"], "credit.pdf", { type: "application/pdf" });
    await userEvent.upload(screen.getByLabelText(/File \(PDF/), file);
    await userEvent.click(screen.getByRole("button", { name: "Send" }));
    expect(submit).not.toHaveBeenCalled();
    await userEvent.selectOptions(screen.getByLabelText(/Which invoice/), INVOICE_ID);
    await userEvent.click(screen.getByRole("button", { name: "Send" }));
    expect(submit).toHaveBeenCalledWith(file, "credit_note", INVOICE_ID);
    expect(await screen.findByText("Received. Reference AP-CREDIT1.")).toBeInTheDocument();
  });
});

describe("vendor invoice detail", () => {
  it("threads messages with accounts payable and adds a new one", async () => {
    const detail: VendorPortalInvoiceDetail = {
      ...invoice(),
      thread: [{ id: "m1", from_vendor: false, author: "", text: "It is in the next run.", at: "2026-10-01T10:00:00Z" }],
      decisions: [],
    };
    const write = vi.fn().mockResolvedValue({
      ok: true,
      invoice: { ...detail, thread: [...detail.thread, { id: "m2", from_vendor: true, author: "Huda Rahman", text: "Thank you.", at: "2026-10-01T11:00:00Z" }] },
    });
    renderWithIntl(<VendorInvoiceDetail invoice={detail} write={write} />);
    expect(screen.getByText("It is in the next run.")).toBeInTheDocument();
    await userEvent.type(screen.getByLabelText(/Write to accounts payable/), "Thank you.");
    await userEvent.click(screen.getByRole("button", { name: "Send" }));
    expect(write).toHaveBeenCalledWith(INVOICE_ID, "Thank you.");
    expect(await screen.findByText("Thank you.")).toBeInTheDocument();
  });
});

describe("vendor invoice review requests", () => {
  const DECISION = "dec_01JABCDEFGHJKMNPQRSTVWXYZ0";
  const row: VendorPortalDecision = { decision_id: DECISION, kind: "rejection", decided_at: "2026-10-02T09:00:00Z", can_appeal: true, appeal: null };
  const rejected: VendorPortalInvoiceDetail = { ...invoice({ status: "rejected" }), thread: [], decisions: [row] };

  it("asks a person to review a rejection and shows the deadline", async () => {
    const ask = vi.fn().mockResolvedValue({
      ok: true,
      invoice: {
        ...rejected,
        decisions: [
          {
            ...row,
            can_appeal: false,
            appeal: {
              id: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
              reference: "APL-3F2A9C1D",
              status: "open",
              outcome: null,
              sla_due_at: "2026-10-16T09:00:00Z",
              created_at: "2026-10-02T10:00:00Z",
            },
          },
        ],
      },
    });
    renderWithIntl(<VendorInvoiceDetail invoice={rejected} ask={ask} />);
    expect(screen.getByText("Invoice rejected")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Ask for a review" }));
    await userEvent.type(screen.getByLabelText(/looked at again/), "We sent it once only.");
    await userEvent.click(screen.getByRole("button", { name: "Send the request" }));
    expect(ask).toHaveBeenCalledWith(INVOICE_ID, DECISION, "We sent it once only.");
    expect(await screen.findByText(/Review APL-3F2A9C1D asked/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Ask for a review" })).not.toBeInTheDocument();
  });

  it("shows a resolved review in Arabic and nothing when there is no decision to review", () => {
    const resolved: VendorPortalInvoiceDetail = {
      ...rejected,
      decisions: [
        {
          ...row,
          kind: "duplicate_flag",
          can_appeal: false,
          appeal: {
            id: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
            reference: "APL-3F2A9C1D",
            status: "resolved",
            outcome: "overturned",
            sla_due_at: "2026-10-16T09:00:00Z",
            created_at: "2026-10-02T10:00:00Z",
          },
        },
      ],
    };
    const { unmount } = renderWithIntl(<VendorInvoiceDetail invoice={resolved} />, "ar");
    expect(screen.getByText("عُلّمت كفاتورة مكررة محتملة")).toBeInTheDocument();
    expect(screen.getByText(/تم تغيير القرار/)).toBeInTheDocument();
    unmount();
    renderWithIntl(<VendorInvoiceDetail invoice={{ ...rejected, decisions: [] }} />);
    expect(screen.queryByText("Ask for a human review")).not.toBeInTheDocument();
  });
});

describe("vendor details", () => {
  it("shows the bank account masked and explains that a change is only a request", async () => {
    const requestChange = vi.fn().mockResolvedValue({
      ok: true,
      request: { reference: "BCT-4M8T2X", status: "awaiting_verification", source: "vendor_portal", iban_last4: "0042", created_at: "2026-10-14T10:00:00Z" },
    });
    renderWithIntl(<VendorDetails details={DETAILS} actions={{ requestChange }} />);
    expect(screen.getByText("AE•• •••• •••• 1715")).toBeInTheDocument();
    expect(document.querySelector("[data-bank-explanation]")?.textContent).toMatch(/never change here directly/);
    await userEvent.type(screen.getByLabelText("IBAN"), "AE070331234567890120042");
    await userEvent.type(screen.getAllByLabelText("Name on the account")[0]!, "Gulf Office Supplies LLC");
    await userEvent.click(screen.getByRole("button", { name: "Send the request" }));
    expect(requestChange).toHaveBeenCalled();
    expect(await screen.findByText(/Request BCT-4M8T2X is open/)).toBeInTheDocument();
    expect(document.querySelector("[data-vendor-hold]")).not.toBeNull();
    expect(document.querySelector('[data-open-request="BCT-4M8T2X"]')).not.toBeNull();
  });

  it("shows contacts read only and never offers to change them or the number on file", () => {
    // Security review 2026-10, L-10: accounts payable writes to these addresses, so only
    // accounts payable changes them.
    renderWithIntl(<VendorDetails details={DETAILS} actions={{ requestChange: vi.fn() }} />);
    const row = document.querySelector('[data-contact-row="0"]');
    expect(row?.textContent).toContain("Huda Rahman");
    expect(row?.textContent).toContain("huda@gulf-office.example");
    expect(document.querySelector("[data-contacts-form]")).toBeNull();
    expect(screen.queryByRole("button", { name: "Save contacts" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Add a contact" })).toBeNull();
    expect(screen.queryByLabelText("Email")).toBeNull();
    expect(screen.queryByLabelText(/phone number on file/i)).toBeNull();
    expect(screen.getByText(/only they can change it/)).toBeInTheDocument();
    expect(screen.getByText(/To add or change a contact/)).toBeInTheDocument();
  });
});
