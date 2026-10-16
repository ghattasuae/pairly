// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
import { screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { PayEarlyPayment } from "@/lib/pay/types";

import { renderWithIntl } from "../../../test/intl";

const { EarlyPaymentNote } = await import("../pay/early-payment");

function offer(patch: Partial<PayEarlyPayment> = {}): PayEarlyPayment {
  return {
    terms: "2/10 net 30",
    source: "vendor",
    percent: "2",
    currency: "AED",
    amount: "1050.00",
    saving: "21.00",
    pay_early: "1029.00",
    pay_by: "2026-10-25",
    net_due: "2026-11-14",
    annual_rate: "0.3724",
    available: true,
    days_left: 3,
    ...patch,
  };
}

describe("EarlyPaymentNote", () => {
  it("shows the saving, the pay-by date and where the terms come from", () => {
    renderWithIntl(<EarlyPaymentNote offer={offer()} />);
    const note = document.querySelector("[data-early-payment='available']");
    expect(note).toBeTruthy();
    expect(note).toHaveTextContent("Paying early saves money");
    expect(note).toHaveTextContent("pay AED 1029.00 by");
    expect(note).toHaveTextContent("a saving of AED 21.00");
    expect(note).toHaveTextContent("The vendor's agreed terms");
    expect(note).toHaveTextContent("3 days left");
  });

  it("says when the discount period ended, and nothing without an offer", () => {
    const { container } = renderWithIntl(<EarlyPaymentNote offer={null} />);
    expect(container).toBeEmptyDOMElement();
    renderWithIntl(<EarlyPaymentNote offer={offer({ available: false, days_left: -2 })} />);
    expect(document.querySelector("[data-early-payment='expired']")).toHaveTextContent("The discount period ended");
  });

  it("reads in Arabic", () => {
    renderWithIntl(<EarlyPaymentNote offer={offer()} />, "ar");
    expect(screen.getByText("الدفع المبكر يوفّر المال")).toBeTruthy();
  });
});
