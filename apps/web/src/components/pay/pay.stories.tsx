// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
import type { Meta, StoryObj } from "@storybook/nextjs-vite";
import type * as React from "react";

import type {
  PayCompare,
  PayCompareSide,
  PayForecast,
  PayForecastBucket,
  PayInvoiceDetail,
  PayInvoiceField,
  PayReconciliation,
  PayRunDetail,
  PayStatementLine,
  PayTicketDetail,
} from "@/lib/pay/types";

import { BankChangeView } from "./bank-change";
import { ComparePanel } from "./invoice-checks";
import { InvoiceReview } from "./invoice-review";
import { PayForecastView } from "./pay-forecast";
import { PayReconciliationView } from "./pay-reconciliation";
import { PaymentRunView } from "./payment-runs";

/*
 * The Pay screens in the "ink and saffron" style, with synthetic data: the invoice viewer
 * with per-field confidence, the duplicate comparison, the bank-detail change ticket at
 * each stage of its lock, a payment run, reconciliation and the cash forecast.
 */

const TEXT =
  "Zarqeth Office Supplies Trading LLC\nTax invoice\nInvoice No: INV-1327/26\nDate: 2026-10-02\nPO: PO-2026-0079\n\n" +
  "1. Toner cartridge   2 x 500.00   1,000.00\n\nSubtotal 1,000.00\nVAT 5% 50.00\nTotal: AED 1,050.00\n\nIBAN AE07 0331 2345 6789 0123 456";

function field(path: string, value: string | null, extra: Partial<PayInvoiceField> = {}): PayInvoiceField {
  const start = value ? TEXT.indexOf(value) : -1;
  return {
    path,
    pattern: path.replace(/\[\d+\]/g, "[]"),
    kind: "text",
    value,
    current_value: value,
    raw_value: null,
    confidence: value ? 0.97 : 0,
    threshold: 0.8,
    required: false,
    needs_correction: false,
    corrected: false,
    flags: [],
    spans: start >= 0 && value ? [{ page: 1, start, end: start + value.length, quote: value, bboxes: [] }] : [],
    ...extra,
  };
}

const INVOICE: PayInvoiceDetail = {
  id: "33333333-3333-4333-8333-333333333333",
  reference: "AP-7K2QX9",
  checks_state: "current",
  status: "needs_review",
  source: "email",
  document_id: "44444444-4444-4444-8444-444444444444",
  vendor: null,
  vendor_name: "Zarqeth Office Supplies Trading LLC",
  invoice_number: "INV-1327/26",
  invoice_date: "2026-10-02",
  due_date: null,
  currency: "AED",
  total: "1050.00",
  review_reasons: ["vendor_not_found", "extraction:low_confidence:total"],
  intake_reference: "REF-0123456789",
  created_at: "2026-10-14T08:00:00Z",
  updated_at: "2026-10-14T08:00:00Z",
  document_type: "invoice",
  credit_for_invoice_id: null,
  credit_total: "0",
  po_number: "PO-2026-0079",
  subtotal: "1000.00",
  vat_amount: "50.00",
  vendor_trn: null,
  sender_domains: [],
  vendor_match: { method: null, conflict: false },
  document: {
    id: "44444444-4444-4444-8444-444444444444",
    media_type: "text/plain",
    original_filename: "email-body.txt",
    status: "parsed",
    sha256: "a".repeat(64),
    text: TEXT,
  },
  extraction: {
    id: "55555555-5555-4555-8555-555555555555",
    status: "needs_human",
    source: "model",
    reasons: [],
    model_name: "m",
    prompt_version: "v1",
    from_recording: true,
    fields: [
      field("invoice_number", "INV-1327/26"),
      field("invoice_date", "2026-10-02", { confidence: 0.84 }),
      field("po_number", "PO-2026-0079", { corrected: true }),
      field("total", "1,050.00", { needs_correction: true, flags: ["low_confidence"], confidence: 0.41 }),
      field("due_date", null, { needs_correction: true, flags: ["missing"], required: true }),
      field("line_items[0].description", "Toner cartridge"),
      field("line_items[0].line_total", "1,000.00"),
    ],
    corrections: [],
    extra_values: {},
  },
  findings: [],
  removed_spans: [],
  email: null,
  decision_ids: [],
  reviewed_at: null,
  rejected_at: null,
  can_process: true,
};

function side(patch: Partial<PayCompareSide> = {}): PayCompareSide {
  return {
    id: "22222222-2222-4222-8222-222222222222",
    reference: "AP-ORIG01",
    status: "ready",
    document_id: "22222222-2222-4222-8222-222222222222",
    vendor_name: "Sesken Telecom Services LLC",
    invoice_number: "INV-2026-01293",
    invoice_date: "2026-06-30",
    due_date: "2026-07-30",
    currency: "AED",
    po_number: null,
    subtotal: "16642.59",
    vat_amount: "832.13",
    total: "17474.72",
    lines: [{ description: "Router rental", quantity: "1", unit_price: "5862.62", line_total: "5862.62" }],
    created_at: "2026-07-01T08:00:00Z",
    ...patch,
  };
}

const COMPARE: PayCompare[] = [
  {
    rules: ["near_number"],
    invoice: side({ id: "33333333-3333-4333-8333-333333333333", reference: "AP-NEW001", invoice_number: "INV-2026-01239" }),
    original: side(),
  },
];

const account = (patch: Partial<PayTicketDetail["proposed_account"]> = {}): PayTicketDetail["proposed_account"] => ({
  id: "11111111-1111-4111-8111-111111111111",
  status: "proposed",
  iban_masked: "AE** **** 3456",
  country: "AE",
  bank_code: "033",
  bank_name: "Noora National Bank (synthetic)",
  swift: "XQNNAEAD",
  account_name: "Seltus IT Solutions LLC",
  created_at: "2026-10-14T08:00:00Z",
  activated_at: null,
  retired_at: null,
  ...patch,
});

function ticket(patch: Partial<PayTicketDetail> = {}, verification: Partial<PayTicketDetail["verification"]> = {}): PayTicketDetail {
  return {
    id: "22222222-2222-4222-8222-222222222222",
    reference: "BCT-7K2QX9",
    vendor_id: "33333333-3333-4333-8333-333333333333",
    vendor_name: "Seltus IT Solutions LLC",
    vendor_reference: "VEN-4M8R2T",
    status: "awaiting_verification",
    source: "email",
    new_iban_masked: "AE** **** 3456",
    created_at: "2026-10-14T08:00:00Z",
    updated_at: "2026-10-14T08:00:00Z",
    activates_at: null,
    outcome: null,
    requested_by_type: "user",
    requested_by_id: "44444444-4444-4444-8444-444444444444",
    requester_user_id: "44444444-4444-4444-8444-444444444444",
    note: "Email from accounts asking to pay a new account.",
    request_had_contact_details: true,
    previous_account: account({ id: "55555555-5555-4555-8555-555555555555", status: "active", iban_masked: "AE** **** 1715", bank_code: "917" }),
    proposed_account: account(),
    proposed_iban: "AE070331234567890123456",
    verification: {
      phone_on_file: "+971 4 555 1629",
      phone_last4: "1629",
      phone_recently_changed: false,
      callback: null,
      callback_required: false,
      verified_by: null,
      verified_at: null,
      calls: [],
      ...verification,
    },
    approval_id: null,
    approval_status: null,
    approval_steps: [],
    approver_ids: [],
    approved_at: null,
    cooling_off_hours: 24,
    completed_at: null,
    closed_at: null,
    hold_kept: null,
    vendor_payment_hold: true,
    vendor_hold_reason: "bank_change",
    decision_ids: [],
    can_verify: true,
    can_cancel: true,
    can_start_approval: false,
    ...patch,
  };
}

const CONFIRMED_CALL = {
  id: "77777777-7777-4777-8777-777777777777",
  result: "confirmed",
  number_last4: "1629",
  notes: "Spoke to the finance manager; the new IBAN was read back and confirmed.",
  created_at: "2026-10-14T10:00:00Z",
} as PayTicketDetail["verification"]["calls"][number];

const COOLING = ticket(
  {
    status: "cooling_off",
    can_verify: false,
    activates_at: "2026-10-15T12:00:00Z",
    approval_steps: [
      { name: "first_approval", status: "approved" },
      { name: "second_approval", status: "approved" },
    ] as PayTicketDetail["approval_steps"],
  },
  { calls: [CONFIRMED_CALL] },
);

const APPROVALS = ticket(
  {
    status: "awaiting_approval",
    can_verify: false,
    approval_id: "66666666-6666-4666-8666-666666666666",
    approval_steps: [
      { name: "first_approval", status: "approved" },
      { name: "second_approval", status: "active" },
    ] as PayTicketDetail["approval_steps"],
  },
  { calls: [CONFIRMED_CALL] },
);

const RUN = {
  id: "77777777-7777-4777-8777-777777777777",
  reference: "PR-AB12CD",
  status: "approved",
  currency: "AED",
  cash_limit: "50000.00",
  total: "28188.43",
  item_count: 2,
  execution_date: "2026-10-20",
  proposed_by: "11111111-1111-4111-8111-111111111111",
  approval_id: "22222222-2222-4222-8222-222222222222",
  approved_at: "2026-10-14T09:00:00Z",
  exported_at: null,
  paid_at: null,
  created_at: "2026-10-14T08:00:00Z",
  due_on_or_before: null,
  approver_ids: ["99999999-9999-4999-8999-999999999999"],
  items: [
    {
      id: "88888888-8888-4888-8888-888888888888",
      position: 1,
      status: "included",
      invoice_id: INVOICE.id,
      invoice_reference: "AP-7K2QX9",
      invoice_number: "INV-1327/26",
      vendor_id: "55555555-5555-4555-8555-555555555555",
      vendor_name: "Zarqeth Office Supplies Trading LLC",
      amount: "1050.00",
      currency: "AED",
      due_date: "2026-10-30",
      discount: "21.00",
      bank_account_masked: "AE** **** 3456",
      end_to_end_id: "PR-AB12CD-0001",
      paid_at: null,
      payment_reference: null,
    },
    {
      id: "88888888-8888-4888-8888-888888888889",
      position: 2,
      status: "included",
      invoice_id: "33333333-3333-4333-8333-333333333334",
      invoice_reference: "AP-7Q2K9D",
      invoice_number: "GOS-2026-0412",
      vendor_id: "55555555-5555-4555-8555-555555555556",
      vendor_name: "Gulf Office Supplies LLC",
      amount: "27138.43",
      currency: "AED",
      due_date: "2026-10-20",
      discount: null,
      bank_account_masked: "AE** **** 1715",
      end_to_end_id: "PR-AB12CD-0002",
      paid_at: null,
      payment_reference: null,
    },
  ],
  excluded: [
    { invoice_id: "66666666-6666-4666-8666-666666666666", reference: "AP-HELD01", amount: "900.00", due_date: null, reasons: ["vendor_held:bank_change"] },
  ],
  exports: [],
  close_reason: null,
  decision_ids: [],
  can_approve: false,
  can_export: true,
  can_mark_paid: false,
  can_cancel: true,
} as PayRunDetail;

const line = (patch: Partial<PayStatementLine> = {}): PayStatementLine => ({
  id: "44444444-4444-4444-8444-444444444444",
  statement_id: "99999999-9999-4999-8999-999999999999",
  statement_reference: "STM-7K2QX9",
  line_no: 2,
  booking_date: "2026-10-21",
  amount: "-1050.00",
  currency: "AED",
  reference: "PMT PR-AB12CD-0001",
  counterparty_iban_masked: "AE** **** 3456",
  status: "matched",
  match_kind: "item",
  unmatched_reason: null,
  match_reasons: ["amount", "reference"],
  payment_run_id: RUN.id,
  payment_run_reference: "PR-AB12CD",
  invoice_id: INVOICE.id,
  invoice_reference: "AP-7K2QX9",
  ...patch,
});

const RECONCILIATION: PayReconciliation = {
  generated_at: "2026-10-22T08:00:00Z",
  days: 5,
  statements: [
    {
      id: "99999999-9999-4999-8999-999999999999",
      reference: "STM-7K2QX9",
      format: "csv",
      file_sha256: "a".repeat(64),
      bank_statement_id: null,
      account_masked: "AE** **** 0001",
      from_date: "2026-10-21",
      to_date: "2026-10-22",
      line_count: 3,
      created_at: "2026-10-22T08:00:00Z",
    },
  ],
  unmatched: [
    line({
      id: "55555555-5555-4555-8555-555555555555",
      status: "unmatched",
      match_kind: null,
      match_reasons: [],
      unmatched_reason: "ambiguous",
      payment_run_id: null,
      payment_run_reference: null,
      invoice_id: null,
      invoice_reference: null,
      reference: "TRANSFER",
      amount: "-4200.00",
    }),
  ],
  unmatched_count: 1,
  matched: [line()],
  matched_count: 1,
  ignored_count: 1,
  unexplained: [
    {
      invoice_id: "66666666-6666-4666-8666-666666666666",
      invoice_reference: "AP-LATE01",
      invoice_number: "A-77",
      vendor_name: "Seltus IT Solutions LLC",
      payment_run_id: RUN.id,
      payment_run_reference: "PR-AB12CD",
      amount: "300.00",
      currency: "AED",
      paid_at: "2026-10-10T08:00:00Z",
      payment_reference: "BANK-1",
      days_since_paid: 12,
    },
  ],
  unexplained_count: 1,
  can_upload: true,
};

const bucket = (patch: Partial<PayForecastBucket> = {}): PayForecastBucket => ({
  amount: "0.00",
  gross: "0.00",
  credits: "0.00",
  count: 0,
  invoices: [],
  ...patch,
});

const AMOUNTS: [string, string][] = [
  ["12400.00", "0.00"],
  ["8200.00", "3100.00"],
  ["27138.43", "1050.00"],
  ["4300.00", "6400.00"],
  ["0.00", "2200.00"],
  ["15800.00", "0.00"],
];

const FORECAST: PayForecast = {
  generated_at: "2026-10-14T08:00:00Z",
  today: "2026-10-14",
  weeks: AMOUNTS.length,
  currencies: [
    {
      currency: "AED",
      committed_total: "67838.43",
      in_review_total: "12750.00",
      weeks: AMOUNTS.map(([committed, review], index) => ({
        index,
        start: new Date(Date.UTC(2026, 9, 12 + index * 7)).toISOString().slice(0, 10),
        end: new Date(Date.UTC(2026, 9, 18 + index * 7)).toISOString().slice(0, 10),
        committed: Number(committed) > 0 ? bucket({ amount: committed, gross: committed, count: 2 }) : bucket(),
        in_review: Number(review) > 0 ? bucket({ amount: review, gross: review, count: 1 }) : bucket(),
      })),
      later_committed: bucket(),
      later_in_review: bucket(),
      undated_committed: bucket(),
      undated_in_review: bucket(),
    },
  ],
  unpriced: [],
};

function Page({ children }: { children: React.ReactNode }) {
  return <div className="page max-w-[88rem]">{children}</div>;
}

const meta = {
  title: "Pay/Screens",
  parameters: { layout: "fullscreen" },
  decorators: [
    (Story) => (
      <Page>
        <Story />
      </Page>
    ),
  ],
} satisfies Meta;

export default meta;
type Story = StoryObj<typeof meta>;

export const InvoiceViewer: Story = { render: () => <InvoiceReview initial={INVOICE} vendors={[]} /> };
export const InvoiceViewerArabicDark: Story = { ...InvoiceViewer, globals: { locale: "ar", theme: "dark" } };

export const DuplicateComparison: Story = { render: () => <ComparePanel compare={COMPARE} /> };
export const DuplicateComparisonDark: Story = { ...DuplicateComparison, globals: { theme: "dark" } };

export const BankChangeLocked: Story = {
  render: () => <BankChangeView initial={ticket()} totpEnabled returnPath="/en/pay/bank-changes/x" />,
};
export const BankChangeLockedArabicDark: Story = { ...BankChangeLocked, globals: { locale: "ar", theme: "dark" } };
export const BankChangeTwoApprovals: Story = {
  render: () => <BankChangeView initial={APPROVALS} totpEnabled returnPath="/en/pay/bank-changes/x" />,
};
export const BankChangeCoolingOff: Story = {
  render: () => <BankChangeView initial={COOLING} totpEnabled returnPath="/en/pay/bank-changes/x" />,
};
export const BankChangeCoolingOffDark: Story = { ...BankChangeCoolingOff, globals: { theme: "dark" } };

export const PaymentRun: Story = { render: () => <PaymentRunView initial={RUN} totpEnabled /> };
export const PaymentRunArabic: Story = { ...PaymentRun, globals: { locale: "ar" } };

export const Reconciliation: Story = { render: () => <PayReconciliationView data={RECONCILIATION} /> };
export const ReconciliationDark: Story = { ...Reconciliation, globals: { theme: "dark" } };

export const CashForecast: Story = { render: () => <PayForecastView data={FORECAST} /> };
export const CashForecastDark: Story = { ...CashForecast, globals: { theme: "dark" } };
