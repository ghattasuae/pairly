// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
import { screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { PayAnalytics, PayMetric } from "@/lib/pay/types";

import { renderWithIntl } from "../../../test/intl";
import { navigationMock } from "../../../test/navigation-mock";

vi.mock("@/i18n/navigation", () => navigationMock);

const { PayAnalyticsView } = await import("../pay/pay-analytics");

const INVOICE = "11111111-1111-4111-8111-111111111111";
const TICKET = "22222222-2222-4222-8222-222222222222";

function metric(key: string, value: number | null, unit: string, patch: Partial<PayMetric> = {}): PayMetric {
  return { key, value, unit, n: value === null ? 0 : 2, records: [], records_total: 0, detail: {}, ...patch };
}

function analytics(patch: Partial<PayAnalytics> = {}): PayAnalytics {
  return {
    generated_at: "2026-10-14T08:00:00Z",
    since: null,
    until: null,
    invoices: 4,
    touchless_rate: metric("touchless_rate", 0.5, "rate", {
      records: [{ kind: "invoice", id: INVOICE, label: "AP-0001" }],
      records_total: 1,
    }),
    cycle_time: [
      metric("cycle.receipt_to_approval", 4, "hours"),
      metric("cycle.approval_to_payment", null, "hours"),
      metric("cycle.receipt_to_payment", 26, "hours"),
    ],
    cost_per_invoice: metric("cost_per_invoice", 0, "usd", { detail: { recorded_mode: true } }),
    exception_rate: [metric("exceptions.all", 0.75, "rate"), metric("exceptions.check.bank", 0.25, "rate")],
    duplicates_prevented: [
      metric("duplicates_prevented", 1, "count"),
      metric("duplicates_prevented_value.AED", 500, "amount", { detail: { currency: "AED", amount: "500.00" } }),
    ],
    bank_changes_blocked: metric("bank_changes_blocked", 1, "count", {
      records: [{ kind: "bank_change", id: TICKET, label: "BCT-1" }],
      records_total: 1,
    }),
    fraud_flags: [metric("fraud_flags", 1, "count"), metric("fraud_flags.rejected", 1, "count")],
    approval_sla_breaches: metric("approval_sla_breaches", null, "count"),
    on_time_payment_rate: metric("on_time_payment_rate", 0.5, "rate"),
    days_payable_outstanding: metric("days_payable_outstanding", 21, "days"),
    open_payables: [
      metric("open_payables.AED", 750, "amount", {
        detail: { currency: "AED", amount: "750.00", gross: "1000.00", credits_applied: "250.00" },
        records: [{ kind: "invoice", id: INVOICE, label: "AP-0001" }],
        records_total: 1,
      }),
    ],
    ...patch,
  };
}

function tile(key: string): HTMLElement {
  const found = document.querySelector<HTMLElement>(`[data-metric="${key}"]`);
  if (!found) throw new Error(key);
  return found;
}

describe("PayAnalyticsView", () => {
  it("shows each number with its records and says no data instead of 0", () => {
    renderWithIntl(<PayAnalyticsView data={analytics()} />);
    expect(tile("touchless_rate")).toHaveTextContent("50%");
    const link = within(tile("touchless_rate")).getByRole("link", { name: "AP-0001" });
    expect(link).toHaveAttribute("href", `/pay/invoices/${INVOICE}`);
    expect(within(tile("bank_changes_blocked")).getByRole("link", { name: "BCT-1" })).toHaveAttribute(
      "href",
      `/pay/bank-changes/${TICKET}`,
    );
    expect(tile("approval_sla_breaches")).toHaveTextContent("No data");
    expect(tile("approval_sla_breaches")).not.toHaveTextContent(/\b0\b/);
    expect(tile("cycle.approval_to_payment")).toHaveTextContent("No data");
    // Recorded mode: 0 and the reason.
    expect(tile("cost_per_invoice")).toHaveTextContent("$0");
    expect(tile("cost_per_invoice")).toHaveTextContent("Recorded mode");
    expect(tile("exceptions.check.bank")).toHaveTextContent("Check: bank details");
    expect(tile("duplicates_prevented_value.AED")).toHaveTextContent("500");
    expect(tile("fraud_flags.rejected")).toHaveTextContent("Rejected");
    // Open payables: net of the credit notes applied, with the invoices behind it.
    expect(tile("open_payables.AED")).toHaveTextContent("750");
    expect(tile("open_payables.AED")).toHaveTextContent("credits applied 250.00");
    expect(within(tile("open_payables.AED")).getByRole("link", { name: "AP-0001" })).toHaveAttribute(
      "href",
      `/pay/invoices/${INVOICE}`,
    );
  });

  it("reads in Arabic, with no data where nothing was counted", () => {
    const empty = metric("touchless_rate", null, "rate");
    renderWithIntl(<PayAnalyticsView data={analytics({ invoices: 0, touchless_rate: empty, duplicates_prevented: [] })} />, "ar");
    expect(tile("touchless_rate")).toHaveTextContent("لا توجد بيانات");
    expect(screen.getByText("لم تُستلم أي فاتورة في هذه الفترة")).toBeInTheDocument();
  });
});
