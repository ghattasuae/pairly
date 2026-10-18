// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { PayMonthEnd } from "@/lib/pay/types";

import { renderWithIntl } from "../../../test/intl";
import { navigationMock } from "../../../test/navigation-mock";

vi.mock("@/i18n/navigation", () => navigationMock);

const { PayMonthEndView } = await import("../pay/pay-month-end");

const INVOICE = "11111111-1111-4111-8111-111111111111";
const PO = "22222222-2222-4222-8222-222222222222";
const EXC = "33333333-3333-4333-8333-333333333333";

function empty(bucket: string) {
  return { bucket, amount: "0", count: 0, invoices: [] };
}

function data(patch: Partial<PayMonthEnd> = {}): PayMonthEnd {
  return {
    period: "2026-09",
    start: "2026-09-01",
    end: "2026-09-30",
    as_of: "2026-09-30T23:59:59Z",
    aging: [
      {
        currency: "AED",
        total: "1050.00",
        count: 1,
        buckets: [
          empty("current"),
          {
            bucket: "1_30",
            amount: "1050.00",
            count: 1,
            invoices: [
              {
                id: INVOICE,
                reference: "INV-ABC123",
                invoice_number: "A-1",
                vendor_name: "Synthetic Supplies LLC",
                status: "approved",
                due_date: "2026-09-10",
                invoice_date: "2026-08-10",
                amount: "1050.00",
              },
            ],
          },
          empty("31_60"),
          empty("61_90"),
          empty("90_plus"),
          empty("undated"),
        ],
      },
    ],
    unpriced: [],
    accruals: [
      {
        currency: "AED",
        total: "11999.97",
        count: 1,
        lines: [
          {
            po_id: PO,
            po_number: "PO-2026-0901",
            vendor_name: "Synthetic Supplies LLC",
            line_no: 1,
            description: "Laptop 14 inch",
            received: "3",
            invoiced: "0",
            quantity: "3",
            unit_price: "3999.99",
            amount: "11999.97",
          },
        ],
      },
    ],
    exceptions: [
      {
        id: EXC,
        reference: "INV-EXC001",
        vendor_name: null,
        currency: "AED",
        total: "10.00",
        received_at: "2026-09-12T08:00:00Z",
        categories: ["no_po"],
      },
    ],
    exceptions_count: 1,
    exception_groups: [{ category: "no_po", count: 1, invoice_ids: [EXC] }],
    can_export_journal: true,
    ...patch,
  };
}

describe("PayMonthEndView", () => {
  it("links every figure to its records", async () => {
    renderWithIntl(<PayMonthEndView data={data()} download={vi.fn()} />);
    expect(document.querySelectorAll("[data-aging][data-empty]")).toHaveLength(5);
    await userEvent.click(screen.getAllByText("AED 1050.00")[0]!);
    expect(screen.getByRole("link", { name: "INV-ABC123" })).toHaveAttribute("href", `/pay/invoices/${INVOICE}`);
    expect(screen.getByRole("link", { name: "PO-2026-0901" })).toHaveAttribute("href", `/pay/purchase-orders#po-${PO}`);
    expect(screen.getByText("received 3, invoiced 0, at 3999.99")).toBeInTheDocument();
    const group = document.querySelector('[data-exception-group="no_po"]');
    expect(group).toHaveAttribute("href", "/pay/exceptions?category=no_po");
    expect(screen.getByRole("link", { name: "INV-EXC001" })).toHaveAttribute("href", `/pay/invoices/${EXC}`);
  });

  it("downloads the report and the journal, and shows a refusal", async () => {
    const download = vi.fn().mockResolvedValueOnce({ ok: true }).mockResolvedValueOnce({ ok: false, code: "second_factor_required" });
    renderWithIntl(<PayMonthEndView data={data()} download={download} />);
    await userEvent.click(screen.getByRole("button", { name: "Download the report (CSV)" }));
    await userEvent.click(screen.getByRole("button", { name: "Export the journal (CSV)" }));
    await waitFor(() => expect(screen.getByRole("alert")).toBeInTheDocument());
    expect(download.mock.calls).toEqual([
      ["report", "2026-09"],
      ["journal", "2026-09"],
    ]);
  });

  it("exports the journal in an accounting system format with the account matches", async () => {
    const download = vi.fn().mockResolvedValue({ ok: false, code: "journal_account_unmapped" });
    renderWithIntl(<PayMonthEndView data={data()} download={download} />);
    const select = screen.getByLabelText("Journal format");
    expect(select).toHaveValue("generic");
    expect(document.querySelector("[data-journal-accounts]")).toBeNull();
    expect(screen.getAllByRole("option").map((o) => o.textContent)).toEqual(["Generic CSV", "Xero", "QuickBooks Online", "Zoho Books"]);

    await userEvent.selectOptions(select, "xero");
    expect(screen.getByText("Accounts in Xero")).toBeInTheDocument();
    await userEvent.type(screen.getByLabelText("Account for AP_CONTROL"), "800");
    await userEvent.type(screen.getByLabelText("Account for BANK"), " 090 ");
    await userEvent.type(screen.getByLabelText("Xero tax rate for every line (optional)"), "No VAT");
    await userEvent.click(screen.getByRole("button", { name: "Export the journal (CSV)" }));
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("no matching account"));
    expect(download.mock.calls).toEqual([
      ["journal", "2026-09", { format: "xero", accounts: { AP_CONTROL: "800", BANK: "090" }, tax_rate: "No VAT" }],
    ]);

    // Zoho Books keeps the accounts but takes no tax rate.
    await userEvent.selectOptions(select, "zoho_books");
    expect(screen.queryByLabelText("Xero tax rate for every line (optional)")).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "Export the journal (CSV)" }));
    await waitFor(() => expect(download).toHaveBeenCalledTimes(2));
    expect(download.mock.calls[1]).toEqual(["journal", "2026-09", { format: "zoho_books", accounts: { AP_CONTROL: "800", BANK: "090" } }]);
  });

  it("offers the formats in Arabic", () => {
    renderWithIntl(<PayMonthEndView data={data()} download={vi.fn()} />, "ar");
    expect(screen.getByLabelText("صيغة ملف القيود")).toHaveValue("generic");
    expect(screen.getByRole("option", { name: "CSV عام" })).toBeInTheDocument();
  });

  it("lists the journal exports of the month with their digest", () => {
    const sha = "ab".repeat(32);
    renderWithIntl(
      <PayMonthEndView
        data={data()}
        exports={[
          {
            id: "44444444-4444-4444-8444-444444444444",
            period: "2026-09",
            format: "generic",
            sha256: sha,
            entry_count: 4,
            line_count: 12,
            exported_by: "55555555-5555-4555-8555-555555555555",
            created_at: "2026-10-02T09:00:00Z",
          },
        ]}
        download={vi.fn()}
      />,
    );
    expect(screen.getByText("4 entries, 12 lines")).toBeInTheDocument();
    expect(document.querySelector('[data-export-format="generic"]')).toHaveTextContent("Generic CSV");
    expect(screen.getByTitle(sha)).toBeInTheDocument();
  });

  it("hides the journal export without the permission and renders in Arabic", () => {
    renderWithIntl(<PayMonthEndView data={data({ can_export_journal: false, aging: [], accruals: [] })} download={vi.fn()} />, "ar");
    expect(screen.queryByRole("button", { name: "تصدير القيود (CSV)" })).not.toBeInTheDocument();
    expect(screen.getByText("لم يكن هناك مستحق في نهاية هذا الشهر.")).toBeInTheDocument();
  });
});
