// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
import { screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { PayForecast, PayForecastBucket } from "@/lib/pay/types";

import { renderWithIntl } from "../../../test/intl";
import { navigationMock } from "../../../test/navigation-mock";

vi.mock("@/i18n/navigation", () => navigationMock);

const { PayForecastView } = await import("../pay/pay-forecast");

const INVOICE = "11111111-1111-4111-8111-111111111111";
const REVIEW = "22222222-2222-4222-8222-222222222222";

function bucket(patch: Partial<PayForecastBucket> = {}): PayForecastBucket {
  return { amount: "0.00", gross: "0.00", credits: "0.00", count: 0, invoices: [], ...patch };
}

function forecast(patch: Partial<PayForecast> = {}): PayForecast {
  const weeks = Array.from({ length: 12 }, (_, index) => {
    const start = new Date(Date.UTC(2026, 9, 12 + index * 7));
    const end = new Date(Date.UTC(2026, 9, 18 + index * 7));
    return {
      index,
      start: start.toISOString().slice(0, 10),
      end: end.toISOString().slice(0, 10),
      committed:
        index === 2
          ? bucket({
              amount: "750.00",
              gross: "1000.00",
              credits: "250.00",
              count: 1,
              invoices: [
                {
                  id: INVOICE,
                  reference: "AP-0001",
                  invoice_number: "INV-1",
                  vendor_name: "Synthetic Supplies LLC",
                  status: "approved",
                  expected_date: "2026-10-27",
                  amount: "750.00",
                  credits: "250.00",
                },
              ],
            })
          : bucket(),
      in_review:
        index === 2
          ? bucket({
              amount: "400.00",
              gross: "400.00",
              count: 1,
              invoices: [
                {
                  id: REVIEW,
                  reference: "AP-0002",
                  invoice_number: null,
                  vendor_name: null,
                  status: "needs_review",
                  expected_date: "2026-10-28",
                  amount: "400.00",
                  credits: "0",
                },
              ],
            })
          : bucket(),
    };
  });
  return {
    generated_at: "2026-10-14T08:00:00Z",
    today: "2026-10-14",
    weeks: 12,
    currencies: [
      {
        currency: "AED",
        committed_total: "750.00",
        in_review_total: "400.00",
        weeks,
        later_committed: bucket(),
        later_in_review: bucket(),
        undated_committed: bucket(),
        undated_in_review: bucket(),
      },
    ],
    unpriced: [],
    ...patch,
  };
}

function cell(key: string): HTMLElement {
  const found = document.querySelector<HTMLElement>(`[data-bucket="${key}"]`);
  if (!found) throw new Error(key);
  return found;
}

describe("PayForecastView", () => {
  it("shows twelve weeks per currency, net of credits, every number linking to invoices", () => {
    renderWithIntl(<PayForecastView data={forecast()} />);
    expect(document.querySelectorAll("[data-forecast-currency='AED'] tbody tr")).toHaveLength(12);
    expect(screen.getByText("This week (and overdue)")).toBeTruthy();
    const committed = cell("AED:2:committed");
    expect(committed).toHaveTextContent("AED 750.00");
    expect(committed).toHaveTextContent("after AED 250.00 of credits");
    expect(within(committed).getByRole("link", { name: "AP-0001" })).toHaveAttribute("href", `/pay/invoices/${INVOICE}`);
    const review = cell("AED:2:in_review");
    expect(review).toHaveTextContent("AED 400.00");
    expect(within(review).getByRole("link", { name: "AP-0002" })).toHaveAttribute("href", `/pay/invoices/${REVIEW}`);
    expect(cell("AED:0:committed")).toHaveAttribute("data-empty");
    expect(within(cell("AED:0:committed")).queryByRole("link")).toBeNull();
  });

  it("says when there is nothing to pay, and reads in Arabic", () => {
    renderWithIntl(<PayForecastView data={forecast({ currencies: [] })} />);
    expect(document.querySelector("[data-pay-forecast] [data-empty]")).toHaveTextContent("Nothing to pay");
    renderWithIntl(<PayForecastView data={forecast()} />, "ar");
    expect(screen.getByText("هذا الأسبوع (والمتأخرة)")).toBeTruthy();
  });
});
