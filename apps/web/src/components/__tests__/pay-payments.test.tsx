// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { PayInvoiceDetail, PayRules, PayRunDetail, PayRunPreview } from "@/lib/pay/types";

import { renderWithIntl } from "../../../test/intl";
import { navigationMock, routerSpies } from "../../../test/navigation-mock";

vi.mock("@/i18n/navigation", () => navigationMock);
vi.mock("next/navigation", () => ({ useRouter: () => routerSpies }));
vi.mock("@/lib/actions/pay", () => ({
  codeInvoice: vi.fn(),
  invoiceAction: vi.fn(),
  previewRun: vi.fn(),
  proposeRun: vi.fn(),
  runAction: vi.fn(),
}));
vi.mock("@/lib/auth/client", () => ({ authClient: { signOut: vi.fn() } }));

const pay = await import("@/lib/actions/pay");
const { InvoiceApprovalPanel } = await import("../pay/invoice-approval");
const { PaymentRunList, PaymentRunView } = await import("../pay/payment-runs");
const { ApprovalRulesView } = await import("../pay/approval-rules");
const { PayApprovalEvidence, isPayContext } = await import("../pay/approval-evidence");

const RUN_ID = "77777777-7777-4777-8777-777777777777";
const INVOICE_ID = "33333333-3333-4333-8333-333333333333";

function invoice(patch: Partial<PayInvoiceDetail> = {}): PayInvoiceDetail {
  return {
    id: INVOICE_ID,
    reference: "AP-7K2QX9",
    status: "ready",
    source: "upload",
    document_id: "44444444-4444-4444-8444-444444444444",
    vendor: null,
    vendor_name: "Synthetic Supplies LLC",
    invoice_number: "INV-7",
    invoice_date: null,
    due_date: null,
    currency: "AED",
    total: "1050.00",
    review_reasons: [],
    intake_reference: null,
    created_at: "2026-10-14T08:00:00Z",
    updated_at: "2026-10-14T08:00:00Z",
    po_number: null,
    subtotal: null,
    vat_amount: null,
    vendor_trn: null,
    sender_domains: [],
    vendor_match: {},
    document: {
      id: "44444444-4444-4444-8444-444444444444",
      media_type: "application/pdf",
      original_filename: "a.pdf",
      status: "parsed",
      sha256: "a".repeat(64),
    },
    extraction: null,
    findings: [],
    removed_spans: [],
    email: null,
    decision_ids: [],
    reviewed_at: null,
    rejected_at: null,
    checks_state: "current",
    blockers: [],
    approval: null,
    can_process: true,
    ...patch,
  } as PayInvoiceDetail;
}

function run(patch: Partial<PayRunDetail> = {}): PayRunDetail {
  return {
    id: RUN_ID,
    reference: "PR-AB12CD",
    status: "approved",
    currency: "AED",
    cash_limit: "50000.00",
    total: "1050.00",
    item_count: 1,
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
        invoice_id: INVOICE_ID,
        invoice_reference: "AP-7K2QX9",
        invoice_number: "INV-7",
        vendor_id: "55555555-5555-4555-8555-555555555555",
        vendor_name: "Synthetic Supplies LLC",
        amount: "1050.00",
        currency: "AED",
        due_date: "2026-10-30",
        discount: null,
        bank_account_masked: "AE** **** 3456",
        end_to_end_id: "PR-AB12CD-0001",
        paid_at: null,
        payment_reference: null,
      },
    ],
    excluded: [
      {
        invoice_id: "66666666-6666-4666-8666-666666666666",
        reference: "AP-HELD01",
        amount: "900.00",
        due_date: null,
        reasons: ["vendor_held:manual"],
      },
    ],
    exports: [],
    close_reason: null,
    decision_ids: [],
    can_approve: false,
    can_export: true,
    can_mark_paid: false,
    can_cancel: true,
    ...patch,
  } as PayRunDetail;
}

beforeEach(() => {
  vi.mocked(pay.invoiceAction).mockReset();
  vi.mocked(pay.runAction).mockReset();
  vi.mocked(pay.previewRun).mockReset();
  vi.mocked(pay.proposeRun).mockReset();
  routerSpies.push.mockReset();
});

describe("InvoiceApprovalPanel", () => {
  it("sends a ready invoice for approval", async () => {
    const onChange = vi.fn();
    vi.mocked(pay.invoiceAction).mockResolvedValue({ ok: true, invoice: invoice({ status: "awaiting_approval" }) });
    renderWithIntl(<InvoiceApprovalPanel invoice={invoice()} onChange={onChange} />);
    await userEvent.type(screen.getByLabelText("Note for the approvers (optional)"), "Urgent month end");
    await userEvent.click(screen.getByRole("button", { name: "Send for approval" }));
    expect(pay.invoiceAction).toHaveBeenCalledWith(INVOICE_ID, "submit", "Urgent month end");
    await waitFor(() => expect(onChange).toHaveBeenCalled());
  });

  it("shows why it cannot be sent and offers to run stale checks again", async () => {
    vi.mocked(pay.invoiceAction).mockResolvedValue({ ok: true, invoice: invoice() });
    renderWithIntl(
      <InvoiceApprovalPanel
        invoice={invoice({ checks_state: "stale", blockers: ["vendor_held:manual", "checks_stale"] })}
        onChange={vi.fn()}
      />,
    );
    expect(screen.queryByRole("button", { name: "Send for approval" })).toBeNull();
    expect(screen.getByText("Payments to this vendor are on hold")).toBeTruthy();
    expect(screen.getByText("The checks are out of date")).toBeTruthy();
    await userEvent.click(screen.getByRole("button", { name: "Run the checks again" }));
    expect(pay.invoiceAction).toHaveBeenCalledWith(INVOICE_ID, "checks", "");
  });

  it("links to the inbox while waiting and says who approved", () => {
    const { unmount } = renderWithIntl(
      <InvoiceApprovalPanel
        invoice={invoice({
          status: "awaiting_approval",
          approval: {
            submitted_by: null,
            submitted_at: "2026-10-14T08:00:00Z",
            approval_id: "22222222-2222-4222-8222-222222222222",
            run_id: null,
            approved_at: null,
            approved_by_rule: false,
            approver_ids: [],
            approved_amount: null,
            approved_currency: null,
            approved_account_id: null,
          },
        })}
        onChange={vi.fn()}
      />,
    );
    expect(screen.getByRole("link", { name: "Open in the inbox" }).getAttribute("href")).toBe(
      "/inbox/22222222-2222-4222-8222-222222222222",
    );
    unmount();
    renderWithIntl(
      <InvoiceApprovalPanel
        invoice={invoice({
          status: "approved",
          approval: {
            submitted_by: null,
            submitted_at: "2026-10-14T08:00:00Z",
            approval_id: null,
            run_id: null,
            approved_at: "2026-10-14T09:00:00Z",
            approved_by_rule: true,
            approver_ids: [],
            approved_amount: "1050.00",
            approved_currency: "AED",
            approved_account_id: null,
          },
        })}
        onChange={vi.fn()}
      />,
    );
    expect(document.querySelector("[data-approved='rule']")).toBeTruthy();
  });
});

describe("PaymentRunList", () => {
  it("previews what a run pays and leaves out, then proposes it", async () => {
    const preview: PayRunPreview = {
      currency: "AED",
      cash_limit: "5000.00",
      total: "1050.00",
      include_early_payment: false,
      savings: "0.00",
      savings_available: "0.00",
      items: [
        {
          invoice_id: INVOICE_ID,
          reference: "AP-7K2QX9",
          vendor_name: "Synthetic Supplies LLC",
          amount: "1050.00",
          due_date: "2026-10-30",
          discount: null,
          saving: null,
          pay_by: null,
        },
      ],
      excluded: [{ invoice_id: "66666666-6666-4666-8666-666666666666", reference: "AP-BIG", amount: "9000.00", due_date: null, reasons: ["over_cash_limit"] }],
    };
    vi.mocked(pay.previewRun).mockResolvedValue({ ok: true, preview });
    vi.mocked(pay.proposeRun).mockResolvedValue({ ok: true, run: run({ status: "awaiting_approval" }) });
    renderWithIntl(<PaymentRunList items={[]} counts={{}} status={null} canCreate defaultCashLimit="5000" />);
    await userEvent.click(screen.getByRole("button", { name: "Preview" }));
    expect(pay.previewRun).toHaveBeenCalledWith(expect.objectContaining({ currency: "AED", cash_limit: "5000" }));
    expect(await screen.findByText("Over the cash limit of the run")).toBeTruthy();
    await userEvent.click(screen.getByRole("button", { name: "Propose this run" }));
    await waitFor(() => expect(routerSpies.push).toHaveBeenCalledWith(`/pay/payment-runs/${RUN_ID}`));
  });

  it("highlights early-payment savings and takes them only when asked", async () => {
    const preview: PayRunPreview = {
      currency: "AED",
      cash_limit: "5000.00",
      total: "1029.00",
      include_early_payment: true,
      savings: "21.00",
      savings_available: "41.00",
      items: [
        {
          invoice_id: INVOICE_ID,
          reference: "AP-7K2QX9",
          vendor_name: "Synthetic Supplies LLC",
          amount: "1029.00",
          due_date: "2026-11-30",
          discount: "21.00",
          saving: "21.00",
          pay_by: "2026-10-25",
        },
      ],
      excluded: [
        {
          invoice_id: "66666666-6666-4666-8666-666666666666",
          reference: "AP-HELD",
          amount: "1000.00",
          due_date: "2026-11-30",
          reasons: ["vendor_held:manual"],
          saving: "20.00",
          pay_by: "2026-10-25",
        },
      ],
    };
    vi.mocked(pay.previewRun).mockResolvedValue({ ok: true, preview });
    renderWithIntl(<PaymentRunList items={[]} counts={{}} status={null} canCreate defaultCashLimit="5000" />);
    await userEvent.click(screen.getByLabelText("Take early-payment discounts"));
    await userEvent.click(screen.getByRole("button", { name: "Preview" }));
    expect(pay.previewRun).toHaveBeenCalledWith(expect.objectContaining({ include_early_payment: true }));
    expect(await screen.findByText("Discounts taken: AED 21.00")).toBeTruthy();
    expect(screen.getByText(/discount 21.00/)).toBeTruthy();
    // Still subject to holds: left out, with what it would have saved.
    expect(screen.getByText("Payments to this vendor are on hold")).toBeTruthy();
    expect(screen.getByText("Save 20.00 if paid by 2026-10-25")).toBeTruthy();
  });

  it("hides the proposal without the permission", () => {
    renderWithIntl(<PaymentRunList items={[run()]} counts={{ approved: 1 }} status={null} canCreate={false} defaultCashLimit={null} />);
    expect(screen.queryByRole("button", { name: "Preview" })).toBeNull();
    expect(document.querySelector("[data-run='PR-AB12CD']")).toBeTruthy();
  });
});

describe("PaymentRunView", () => {
  it("shows what changed when the bank file is refused", async () => {
    const download = vi.fn().mockResolvedValue({
      ok: false,
      code: "export_refused",
      problems: ["PR-AB12CD-0001:bank_details_changed", "PR-AB12CD-0001:vendor_held:bank_change"],
    });
    renderWithIntl(<PaymentRunView initial={run()} totpEnabled download={download} />);
    expect(screen.getByText("Payments to this vendor are on hold")).toBeTruthy(); // the excluded invoice
    await userEvent.click(screen.getByRole("button", { name: "Download ISO 20022 XML" }));
    expect(download).toHaveBeenCalledWith(RUN_ID, "pain001");
    const problems = await screen.findByText("No file was made. This changed since the approval:");
    expect(problems).toBeTruthy();
    expect(screen.getByText("The bank details changed since the approval")).toBeTruthy();
  });

  it("asks for the second factor", async () => {
    const download = vi.fn().mockResolvedValue({ ok: false, code: "second_factor_required", problems: [] });
    renderWithIntl(<PaymentRunView initial={run()} totpEnabled download={download} />);
    await userEvent.click(screen.getByRole("button", { name: "Download CSV" }));
    await waitFor(() => expect(download).toHaveBeenCalledWith(RUN_ID, "csv"));
    expect(document.querySelector("[data-export-problems]")).toBeNull();
  });

  it("marks an exported run paid with the bank reference", async () => {
    vi.mocked(pay.runAction).mockResolvedValue({ ok: true, run: run({ status: "paid", can_mark_paid: false }) });
    renderWithIntl(<PaymentRunView initial={run({ status: "exported", can_mark_paid: true })} totpEnabled download={vi.fn()} />);
    await userEvent.type(screen.getByLabelText("Bank reference"), "BANK-REF-1");
    await userEvent.click(screen.getByRole("button", { name: "Mark the rest paid" }));
    expect(pay.runAction).toHaveBeenCalledWith(RUN_ID, "paid", "BANK-REF-1");
    await waitFor(() => expect(document.querySelector("[data-run-view='paid']")).toBeTruthy());
  });

  it("reads in Arabic", () => {
    renderWithIntl(<PaymentRunView initial={run()} totpEnabled download={vi.fn()} />, "ar");
    expect(screen.getByRole("button", { name: "نزّل CSV" })).toBeTruthy();
  });
});

describe("ApprovalRulesView", () => {
  it("shows the active YAML, its chains and versions", () => {
    const items: PayRules[] = [
      {
        approval_type: "pay.invoice",
        title: "Invoice approval",
        active: {
          id: "a1a1a1a1-a1a1-4a1a-8a1a-a1a1a1a1a1a1",
          version: 2,
          content: "# rules\nchains:\n  - name: high_risk\n  - name: standard\n",
          content_hash: "f".repeat(64),
          created_at: "2026-10-14T08:00:00Z",
        },
        default_content: "chains:\n  - name: standard\n",
        versions: [
          { id: "a1a1a1a1-a1a1-4a1a-8a1a-a1a1a1a1a1a1", version: 2, content: "", content_hash: "f".repeat(64), created_at: "2026-10-14T08:00:00Z" },
          { id: "b1b1b1b1-b1b1-4b1b-8b1b-b1b1b1b1b1b1", version: 1, content: "", content_hash: "e".repeat(64), created_at: "2026-10-13T08:00:00Z" },
        ],
      },
      { approval_type: "pay.payment_run", title: "Payment run", active: null, default_content: "chains:\n  - name: finance_controller\n", versions: [] },
    ];
    renderWithIntl(<ApprovalRulesView items={items} />);
    expect(screen.getByText("Version 2")).toBeTruthy();
    expect(screen.getByText("high_risk")).toBeTruthy();
    expect(screen.getByText("Not used yet")).toBeTruthy();
    expect(document.querySelectorAll("[data-rules-versions] li")).toHaveLength(2);
    expect(document.querySelector("[data-rules='pay.payment_run'] [data-rules-yaml]")?.textContent).toContain("finance_controller");
  });
});

describe("PayApprovalEvidence", () => {
  it("shows the invoice values, risk, findings and suspected originals", () => {
    const context = {
      kind: "pay.invoice",
      invoice_path: `/pay/invoices/${INVOICE_ID}`,
      fields: { vendor_name: "Synthetic Supplies LLC", total: "1050.00", department: "marketing" },
      risk: { level: "high", explanation: { en: "Bank details differ from the vendor master.", ar: "تختلف البيانات المصرفية." } },
      vendor: { risk: "medium" },
      findings: [
        { code: "bank_mismatch", needs_review: true, message: { en: "The IBAN is not the one on file.", ar: "رقم الآيبان غير المسجل." } },
        { code: "no_policy_found", needs_review: false, message: { en: "No policy found.", ar: "لا توجد سياسة." } },
      ],
      compare: [{ original_id: "x", rules: ["near_duplicate"] }],
    };
    expect(isPayContext(context)).toBe(true);
    expect(isPayContext({ kind: "hire.offer" })).toBe(false);
    renderWithIntl(<PayApprovalEvidence context={context} />);
    expect(screen.getByText("Synthetic Supplies LLC")).toBeTruthy();
    expect(screen.getByText("marketing")).toBeTruthy();
    expect(screen.getByText("High")).toBeTruthy();
    expect(screen.getByText("Bank details differ from the vendor master.")).toBeTruthy();
    expect(screen.getByText("The IBAN is not the one on file.")).toBeTruthy();
    expect(screen.queryByText("No policy found.")).toBeNull();
    expect(screen.getByRole("link", { name: "Open the invoice side by side" }).getAttribute("href")).toBe(
      `/pay/invoices/${INVOICE_ID}`,
    );
  });

  it("never links outside Pay", () => {
    renderWithIntl(<PayApprovalEvidence context={{ kind: "pay.invoice", invoice_path: "https://evil.example/x" }} />);
    expect(screen.queryByRole("link")).toBeNull();
  });
});
