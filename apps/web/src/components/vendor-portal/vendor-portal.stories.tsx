// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
import type { Meta, StoryObj } from "@storybook/nextjs-vite";
import type * as React from "react";

import type {
  VendorPortalDetails,
  VendorPortalInvoice,
  VendorPortalInvoiceDetail,
  VendorPortalMe,
} from "@/lib/vendor-portal/types";

import { VendorDetails } from "./vendor-details";
import { VendorPortalHome } from "./vendor-home";
import { VendorInvoiceDetail } from "./vendor-invoice-detail";

/* The vendor portal, the public paper-and-ink face of Pay, with synthetic data. */

const ME: VendorPortalMe = {
  organisation: "Acme Trading",
  org_id: "0123456789abcdef0123456789abcdef",
  vendor_name: "Gulf Office Supplies LLC",
  vendor_reference: "VEN-7K2QX9",
  contact_name: "Huda Rahman",
  email: "huda@gulf-office.example",
  locale: "en",
};

function invoice(patch: Partial<VendorPortalInvoice> = {}): VendorPortalInvoice {
  return {
    id: "11111111-1111-4111-8111-111111111111",
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
    messages: 2,
    ...patch,
  };
}

const INVOICES = [
  invoice(),
  invoice({
    id: "22222222-2222-4222-8222-222222222222",
    reference: "AP-3M8R2T",
    invoice_number: "GOS-2026-0398",
    status: "in_review",
    expected_payment_date: null,
    total: "4120.00",
    amount_to_pay: "4120.00",
    messages: 0,
    steps: [
      { step: "received", at: "2026-10-12T08:00:00Z" },
      { step: "in_review", at: "2026-10-12T08:02:00Z" },
      { step: "approved", at: null },
      { step: "scheduled", at: null },
      { step: "paid", at: null },
    ],
  }),
];

const DETAIL: VendorPortalInvoiceDetail = {
  ...invoice(),
  thread: [
    { id: "m1", from_vendor: true, author: "Huda Rahman", text: "Could you confirm the payment date?", at: "2026-10-01T09:00:00Z" },
    { id: "m2", from_vendor: false, author: "", text: "It is in the payment run of 22 October.", at: "2026-10-01T10:00:00Z" },
  ],
  decisions: [],
} as VendorPortalInvoiceDetail;

const DETAILS: VendorPortalDetails = {
  legal_name: "Gulf Office Supplies LLC",
  legal_name_ar: null,
  trade_name: null,
  reference: "VEN-7K2QX9",
  country: "AE",
  trn: "100123456700003",
  trn_status: "verified",
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

function Column({ children }: { children: React.ReactNode }) {
  return <div className="bg-ledger mx-auto w-full max-w-3xl px-4 py-8 sm:px-6 sm:py-12">{children}</div>;
}

const meta = {
  title: "Pay/Vendor portal",
  parameters: { layout: "fullscreen" },
  decorators: [
    (Story) => (
      <Column>
        <Story />
      </Column>
    ),
  ],
} satisfies Meta;

export default meta;
type Story = StoryObj<typeof meta>;

export const Home: Story = { render: () => <VendorPortalHome me={ME} invoices={INVOICES} /> };
export const HomeArabicDark: Story = { ...Home, globals: { locale: "ar", theme: "dark" } };
export const InvoiceTimeline: Story = { render: () => <VendorInvoiceDetail invoice={DETAIL} /> };
export const InvoiceTimelineDark: Story = { ...InvoiceTimeline, globals: { theme: "dark" } };
export const VerifiedDetails: Story = { render: () => <VendorDetails details={DETAILS} /> };
export const VerifiedDetailsArabic: Story = { ...VerifiedDetails, globals: { locale: "ar" } };
