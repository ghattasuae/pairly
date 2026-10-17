// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
import { screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { PayDashboard } from "@/lib/pay/dashboards";

import { renderWithIntl } from "../../../test/intl";
import { navigationMock } from "../../../test/navigation-mock";

vi.mock("@/i18n/navigation", () => navigationMock);

const { PayRoleDashboard } = await import("../home/pay-dashboards");

const APPROVAL = "11111111-1111-4111-8111-111111111111";
const INVOICE = "22222222-2222-4222-8222-222222222222";

function approverBoard(): PayDashboard {
  return {
    role: "approver",
    generated_at: "2026-10-16T08:00:00Z",
    action: [
      {
        key: "awaiting_me",
        count: 3,
        href: "/inbox",
        items: [
          {
            kind: "approval",
            id: APPROVAL,
            label: "INV-ABC123",
            href: `/inbox/${APPROVAL}`,
            at: "2026-10-15T08:00:00Z",
            detail: { risk: "high", sla: "overdue", vendor: "Synthetic Supplies LLC", currency: "AED", total: "1050.00" },
          },
        ],
      },
      { key: "delegated", count: 0, href: "/inbox", items: [] },
    ],
    sections: [],
    amounts: [{ key: "approved_this_month", currency: "AED", amount: "1050.00", count: 1, href: "/inbox" }],
    metrics: [
      {
        key: "approved_this_month",
        value: 1,
        unit: "count",
        n: 1,
        records: [{ kind: "invoice", id: INVOICE, label: "INV-ABC123" }],
        records_total: 1,
        detail: {},
      },
    ],
    links: ["inbox", "unknown"],
  };
}

describe("PayRoleDashboard", () => {
  it("shows what waits with risk and SLA, and links every count, amount and record", () => {
    renderWithIntl(<PayRoleDashboard dashboard="approver" data={approverBoard()} />);
    expect(screen.getByRole("heading", { name: "Your Pay approvals" })).toBeInTheDocument();
    expect(screen.getByText("3 things need you")).toBeInTheDocument();
    expect(screen.getAllByRole("link", { name: "INV-ABC123" })[0]).toHaveAttribute("href", `/inbox/${APPROVAL}`);
    expect(screen.getByText("High risk")).toBeInTheDocument();
    expect(screen.getByText("Overdue")).toHaveAttribute("data-sla", "overdue");
    expect(document.querySelector('[data-pay-count="3"]')).toHaveAttribute("href", "/inbox");
    expect(screen.getByText("See all 3")).toBeInTheDocument();
    expect(screen.getByText("AED 1050.00").closest("a")).toHaveAttribute("href", "/inbox");
    expect(document.querySelector('[data-pay-link="unknown"]')).toBeNull();
    expect(document.querySelector('[data-records="approved_this_month"]')).toBeInTheDocument();
  });

  it("renders the finance close checks and aging, in Arabic too", () => {
    const board: PayDashboard = {
      ...approverBoard(),
      role: "finance_controller",
      action: [],
      amounts: [{ key: "aging.31_60", currency: "AED", amount: "10.00", count: 1, href: "/pay/month-end" }],
      sections: [
        {
          key: "month_end",
          count: 1,
          href: "/pay/month-end?period=2026-09",
          items: [
            { kind: "check", id: "journal_exported", label: "journal_exported", href: "/pay/month-end?period=2026-09", at: null, detail: { done: false, count: 0 } },
            { kind: "check", id: "statements_matched", label: "statements_matched", href: "//evil.example", at: null, detail: { done: true, count: 0 } },
          ],
        },
      ],
      metrics: [],
      links: [],
    };
    renderWithIntl(<PayRoleDashboard dashboard="finance_controller" data={board} />);
    expect(screen.getByText("31 to 60 days late")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Journal exported" })).toHaveAttribute("href", "/pay/month-end?period=2026-09");
    expect(screen.queryByRole("link", { name: "Bank statements matched" })).toBeNull(); // not an app path
    expect(document.querySelector('[data-check-done="no"]')).toBeInTheDocument();
  });

  it("renders in Arabic", () => {
    renderWithIntl(<PayRoleDashboard dashboard="approver" data={approverBoard()} />, "ar");
    expect(screen.getByRole("heading", { name: "موافقاتك في الدفع" })).toBeInTheDocument();
    expect(screen.getByText("مخاطر عالية")).toBeInTheDocument();
  });
});
