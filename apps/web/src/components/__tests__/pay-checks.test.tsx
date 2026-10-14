// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
import { screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { PayCheck, PayCompare, PayCompareSide, PayRisk } from "@/lib/pay/types";

import { renderWithIntl } from "../../../test/intl";
import { navigationMock } from "../../../test/navigation-mock";

vi.mock("@/i18n/navigation", () => navigationMock);

const { ComparePanel, InvoiceChecks } = await import("../pay/invoice-checks");
const { ReviewReasons } = await import("../pay/invoice-review");

const ORIGINAL = "11111111-1111-4111-8111-111111111111";

const checks: PayCheck[] = [
  {
    check: "duplicates",
    status: "fail",
    error: null,
    findings: [
      {
        code: "near_duplicate",
        rule: "near_number",
        severity: "high",
        message: {
          en: "Invoice number INV-2026-01239 looks like INV-2026-01293 from this vendor (two digits swapped), with the same total.",
          ar: "رقم الفاتورة INV-2026-01239 يشبه الرقم INV-2026-01293 من هذا المورد.",
        },
        evidence: { original_id: ORIGINAL },
        original_id: ORIGINAL,
        related_ids: [],
        needs_review: true,
      },
    ],
  },
  {
    check: "policy",
    status: "fail",
    error: null,
    findings: [
      {
        code: "no_policy_found",
        rule: "no_policy_found",
        severity: "info",
        message: {
          en: "No spend policy covers this invoice, which is not the same as passing it.",
          ar: "لا توجد سياسة إنفاق تغطي هذه الفاتورة، وهذا لا يعني أنها اجتازت الفحص.",
        },
        evidence: {},
        original_id: null,
        related_ids: [],
        needs_review: false,
      },
    ],
  },
  { check: "forgery", status: "error", error: "document_unavailable", findings: [] },
];

const risk: PayRisk = {
  assessment_id: null,
  score: 0.85,
  level: "high",
  requires_human: true,
  reasons: ["severe_signal:pay.duplicates"],
  explanation: { en: "Risk is high: 1 signals, the strongest from the duplicates check.", ar: "مستوى الخطر مرتفع." },
};

function side(patch: Partial<PayCompareSide> = {}): PayCompareSide {
  return {
    id: ORIGINAL,
    reference: "AP-ORIG01",
    status: "ready",
    document_id: "22222222-2222-4222-8222-222222222222",
    vendor_name: "Sesken Telecom",
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

const compare: PayCompare[] = [
  {
    rules: ["near_number"],
    invoice: side({ id: "33333333-3333-4333-8333-333333333333", reference: "AP-NEW001", invoice_number: "INV-2026-01239" }),
    original: side(),
  },
];

describe("InvoiceChecks", () => {
  it("shows the risk, every finding worst first, and the checks that could not run", () => {
    renderWithIntl(<InvoiceChecks checks={checks} risk={risk} />);
    expect(screen.getByText("High risk")).toBeTruthy();
    expect(screen.getByText("A person must look at this invoice.")).toBeTruthy();
    const list = screen.getByRole("list", { name: "What the checks found" });
    const items = within(list).getAllByRole("listitem");
    expect(items[0]?.getAttribute("data-finding")).toBe("near_duplicate");
    expect(items[1]?.getAttribute("data-finding")).toBe("no_policy_found");
    expect(within(items[0] as HTMLElement).getByText("Open the suspected original").closest("a")?.getAttribute("href")).toBe(
      `/pay/invoices/${ORIGINAL}`,
    );
    expect(screen.getByText("No spend policy covers this invoice, which is not the same as passing it.")).toBeTruthy();
    expect(screen.getByText("The Document check could not run, so a person must look.")).toBeTruthy();
  });

  it("reads in Arabic", () => {
    renderWithIntl(<InvoiceChecks checks={checks} risk={risk} />, "ar");
    expect(screen.getByText("خطر مرتفع")).toBeTruthy();
    expect(screen.getByText("مستوى الخطر مرتفع.")).toBeTruthy();
    expect(screen.getByText("رقم الفاتورة INV-2026-01239 يشبه الرقم INV-2026-01293 من هذا المورد.")).toBeTruthy();
  });

  it("says when nothing was found and renders nothing without checks", () => {
    const { container, unmount } = renderWithIntl(<InvoiceChecks checks={[]} risk={null} />);
    expect(container.querySelector("[data-invoice-checks]")).toBeNull();
    unmount();
    renderWithIntl(
      <InvoiceChecks
        checks={[{ check: "bank", status: "pass", error: null, findings: [] }]}
        risk={{ ...risk, level: "low", requires_human: false }}
      />,
    );
    expect(screen.getByText("The checks found nothing.")).toBeTruthy();
    expect(screen.getByText("Low risk")).toBeTruthy();
  });
});

describe("ComparePanel", () => {
  it("puts this invoice and the suspected original side by side and marks the differences", () => {
    renderWithIntl(<ComparePanel compare={compare} />);
    expect(screen.getByText("Possible duplicate")).toBeTruthy();
    expect(screen.getByText("1 field differs.")).toBeTruthy();
    const number = document.querySelector('[data-compare-row="invoice_number"]') as HTMLElement;
    expect(number.getAttribute("data-same")).toBe("false");
    expect(within(number).getByText("INV-2026-01239")).toBeTruthy();
    expect(within(number).getByText("INV-2026-01293")).toBeTruthy();
    expect(document.querySelector('[data-compare-row="total"]')?.getAttribute("data-same")).toBe("true");
    expect(document.querySelector('[data-compare-line="0"]')?.getAttribute("data-same")).toBe("true");
    expect(screen.getByText("Open AP-ORIG01").closest("a")?.getAttribute("href")).toBe(`/pay/invoices/${ORIGINAL}`);
  });

  it("renders nothing without a suspected original", () => {
    const { container } = renderWithIntl(<ComparePanel compare={[]} />);
    expect(container.querySelector("[data-compare-panel]")).toBeNull();
  });
});

describe("ReviewReasons", () => {
  it("says what a check found in plain words and keeps the code as secondary text", () => {
    renderWithIntl(
      <ReviewReasons reasons={["check:near_duplicate", "check:bank_mismatch", "risk:critical"]} checks={checks} />,
    );
    const items = screen.getAllByRole("listitem");
    // The finding's own explanation, from the checks of the detail.
    expect(items[0]).toHaveTextContent("Invoice number INV-2026-01239 looks like INV-2026-01293");
    // No finding in the detail: the plain sentence of the code.
    expect(items[1]).toHaveTextContent("bank details on the invoice differ from the vendor master");
    expect(items[2]).toHaveTextContent("The risk score needs a person");
    for (const item of items) expect(item).not.toHaveTextContent("A check found something to look at");
    const code = within(items[1]).getByText("(bank_mismatch)");
    expect(code).toHaveAttribute("title", "check:bank_mismatch");
  });

  it("reads in Arabic", () => {
    renderWithIntl(<ReviewReasons reasons={["check:bank_mismatch"]} />, "ar");
    expect(screen.getByRole("listitem")).toHaveTextContent("البيانات المصرفية في الفاتورة تختلف عن سجل المورد");
  });
});
