// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { PayReconciliation, PayStatementLine } from "@/lib/pay/types";

import { renderWithIntl } from "../../../test/intl";
import { navigationMock, routerSpies } from "../../../test/navigation-mock";

vi.mock("@/i18n/navigation", () => navigationMock);
vi.mock("next/navigation", () => ({ useRouter: () => routerSpies }));
vi.mock("@/lib/actions/pay", () => ({
  importStatement: vi.fn(),
  matchStatementsNow: vi.fn(),
}));

const pay = await import("@/lib/actions/pay");
const { PayReconciliationView } = await import("../pay/pay-reconciliation");

const INVOICE = "11111111-1111-4111-8111-111111111111";
const RUN = "22222222-2222-4222-8222-222222222222";
const STATEMENT = "33333333-3333-4333-8333-333333333333";

function line(patch: Partial<PayStatementLine> = {}): PayStatementLine {
  return {
    id: "44444444-4444-4444-8444-444444444444",
    statement_id: STATEMENT,
    statement_reference: "STM-7K2QX9",
    line_no: 2,
    booking_date: "2026-10-21",
    amount: "-1050.00",
    currency: "AED",
    reference: "PMT E2E-0001",
    counterparty_iban_masked: "AE** **** 3456",
    status: "matched",
    match_kind: "item",
    unmatched_reason: null,
    match_reasons: ["amount", "reference"],
    payment_run_id: RUN,
    payment_run_reference: "RUN-4M8R2T",
    invoice_id: INVOICE,
    invoice_reference: "INV-ABC123",
    ...patch,
  };
}

function data(patch: Partial<PayReconciliation> = {}): PayReconciliation {
  return {
    generated_at: "2026-10-22T08:00:00Z",
    days: 5,
    statements: [
      {
        id: STATEMENT,
        reference: "STM-7K2QX9",
        format: "csv",
        file_sha256: "a".repeat(64),
        bank_statement_id: null,
        account_masked: null,
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
      }),
    ],
    unmatched_count: 1,
    matched: [line()],
    matched_count: 1,
    ignored_count: 1,
    unexplained: [
      {
        invoice_id: "66666666-6666-4666-8666-666666666666",
        invoice_reference: "INV-LATE01",
        invoice_number: "A-77",
        vendor_name: "Seltus IT Solutions LLC (synthetic)",
        payment_run_id: RUN,
        payment_run_reference: "RUN-4M8R2T",
        amount: "300.00",
        currency: "AED",
        paid_at: "2026-10-10T08:00:00Z",
        payment_reference: "BANK-1",
        days_since_paid: 12,
      },
    ],
    unexplained_count: 1,
    can_upload: true,
    ...patch,
  };
}

describe("PayReconciliationView", () => {
  it("links every match and every unexplained payment to its invoice and run", () => {
    renderWithIntl(<PayReconciliationView data={data()} />);
    expect(screen.getByRole("link", { name: "INV-ABC123" })).toHaveAttribute("href", `/pay/invoices/${INVOICE}`);
    expect(screen.getAllByRole("link", { name: "RUN-4M8R2T" })[0]).toHaveAttribute("href", `/pay/payment-runs/${RUN}`);
    expect(screen.getByRole("link", { name: "INV-LATE01" })).toBeInTheDocument();
    expect(screen.getByText("paid 12 days ago")).toBeInTheDocument();
    expect(screen.getByText("amount, reference")).toBeInTheDocument();
    expect(screen.getByText(/Several payments fit equally well/)).toBeInTheDocument();
    for (const key of ["matched", "unmatched", "unexplained", "ignored"]) {
      expect(document.querySelector(`[data-count="${key}"] a`)).toHaveAttribute("href", `#${key}`);
    }
  });

  it("imports a CSV statement and shows the result", async () => {
    vi.mocked(pay.importStatement).mockResolvedValue({ ok: true, reference: "STM-NEW001", lines: 4, matched: 2, unmatched: 1 });
    renderWithIntl(<PayReconciliationView data={data()} />);
    const file = new File(["date,amount,currency,reference,counterparty_iban\n"], "october.csv", { type: "text/csv" });
    await userEvent.upload(screen.getByLabelText("Statement file"), file);
    await userEvent.click(screen.getByRole("button", { name: "Import" }));
    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("STM-NEW001"));
    expect(vi.mocked(pay.importStatement).mock.calls[0]![0]).toBe("csv");
    expect(routerSpies.refresh).toHaveBeenCalled();
  });

  it("lists every problem of a refused statement", async () => {
    vi.mocked(pay.importStatement).mockResolvedValue({
      ok: false,
      code: "invalid_statement",
      problems: [
        { row: 2, field: "amount", code: "invalid_amount" },
        { row: 0, field: null, code: "malformed_xml" },
      ],
    });
    renderWithIntl(<PayReconciliationView data={data()} />);
    await userEvent.upload(screen.getByLabelText("Statement file"), new File(["<x"], "s.xml", { type: "text/xml" }));
    await userEvent.click(screen.getByRole("button", { name: "Import" }));
    expect(await screen.findByText(/Nothing was imported/)).toBeInTheDocument();
    expect(vi.mocked(pay.importStatement).mock.calls.at(-1)![0]).toBe("camt053");
    expect(document.querySelector('[data-statement-problem="2:invalid_amount"]')).toBeInTheDocument();
    expect(document.querySelector('[data-statement-problem="0:malformed_xml"]')).toBeInTheDocument();
  });

  it("hides the import for readers and renders in Arabic", () => {
    renderWithIntl(<PayReconciliationView data={data({ can_upload: false })} />, "ar");
    expect(screen.queryByRole("button", { name: "استيراد" })).not.toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "أحدث المطابقات" })).toBeInTheDocument();
  });
});
