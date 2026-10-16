// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
import { fireEvent, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { PayExceptionItem, PayExceptions, PayMessageDraft } from "@/lib/pay/types";

import { renderWithIntl } from "../../../test/intl";
import { navigationMock, routerSpies } from "../../../test/navigation-mock";

vi.mock("@/i18n/navigation", () => navigationMock);
vi.mock("next/navigation", () => ({ useRouter: () => routerSpies }));
vi.mock("@/lib/actions/pay", () => ({ draftExceptionMessage: vi.fn(), sendExceptionMessage: vi.fn() }));

const pay = await import("@/lib/actions/pay");
const { ExceptionsQueue } = await import("../pay/exceptions");

const FIRST = "11111111-1111-4111-8111-111111111111";
const SECOND = "22222222-2222-4222-8222-222222222222";

function item(id: string, reference: string, patch: Partial<PayExceptionItem> = {}): PayExceptionItem {
  return {
    id,
    reference,
    status: "needs_review",
    vendor_name: "Synthetic Supplies LLC",
    invoice_number: `INV-${reference}`,
    po_number: "PO-2026-0001",
    currency: "AED",
    total: "1250.00",
    created_at: "2026-10-10T08:00:00Z",
    age_hours: 75,
    overdue: true,
    categories: [
      {
        category: "missing_receipt",
        explanations: [{ en: "No goods receipt is on file.", ar: "لا يوجد إيصال استلام مسجل." }],
        resolution: { en: "Record the goods receipt.", ar: "سجل إيصال الاستلام." },
        templates: [
          { id: "delivery_note", recipient: "vendor" },
          { id: "record_receipt", recipient: "requester" },
        ],
      },
    ],
    ...patch,
  };
}

function data(patch: Partial<PayExceptions> = {}): PayExceptions {
  return {
    items: [
      item(FIRST, "AP-AAA111"),
      item(SECOND, "AP-BBB222", {
        age_hours: 5,
        overdue: false,
        categories: [
          {
            category: "bank_mismatch",
            explanations: [{ en: "The bank details differ.", ar: "البيانات البنكية مختلفة." }],
            resolution: { en: "Call the vendor on the number on file.", ar: "اتصل بالمورد." },
            templates: [],
          },
        ],
      }),
    ],
    counts: {
      no_po: 0,
      price_variance: 0,
      quantity_variance: 0,
      missing_receipt: 1,
      duplicate_suspect: 0,
      bank_mismatch: 1,
      policy_breach: 0,
      low_confidence: 0,
      other: 0,
    },
    total: 2,
    overdue: 1,
    sla_hours: 48,
    can_message: true,
    ...patch,
  };
}

function draft(patch: Partial<PayMessageDraft> = {}): PayMessageDraft {
  return {
    category: "missing_receipt",
    template_id: "delivery_note",
    recipient: "vendor",
    locale: "en",
    message: "Please send the delivery note.\n\nThank you for your help.",
    template_text: "Please send the delivery note.",
    ai_note: "Thank you for your help.",
    ai_problems: [],
    from_recording: true,
    model_call_id: "33333333-3333-4333-8333-333333333333",
    contacts: [{ name: "Amal", email: "amal@vendor.example" }],
    requester_hint: null,
    ...patch,
  };
}

beforeEach(() => {
  vi.mocked(pay.draftExceptionMessage).mockReset();
  vi.mocked(pay.sendExceptionMessage).mockReset();
  routerSpies.push.mockReset();
});

describe("ExceptionsQueue", () => {
  it("lists categories, explanations, resolutions, counts and age", () => {
    renderWithIntl(<ExceptionsQueue data={data()} locale="en" initialCategory={null} />);
    const first = document.querySelector('[data-exception="AP-AAA111"]');
    expect(first?.textContent).toContain("Missing receipt");
    expect(first?.textContent).toContain("No goods receipt is on file.");
    expect(first?.textContent).toContain("Record the goods receipt.");
    expect(first?.textContent).toContain("3 d 3 h");
    expect(first?.querySelector("[data-overdue]")).not.toBeNull();
    expect(screen.getByText("2 invoices waiting")).toBeTruthy();
    expect(document.querySelector('[data-filter="missing_receipt"]')?.textContent).toContain("1");
    // A bank mismatch has no message: those vendors are called, never emailed.
    const second = document.querySelector('[data-exception="AP-BBB222"]');
    expect(second?.textContent).not.toContain("Write a message");
  });

  it("filters by category", async () => {
    renderWithIntl(<ExceptionsQueue data={data()} locale="en" initialCategory={null} />);
    await userEvent.click(screen.getByRole("button", { name: /Bank mismatch/ }));
    expect(document.querySelector('[data-exception="AP-AAA111"]')).toBeNull();
    expect(document.querySelector('[data-exception="AP-BBB222"]')).not.toBeNull();
  });

  it("moves with J and K and opens with Enter", () => {
    renderWithIntl(<ExceptionsQueue data={data()} locale="en" initialCategory={null} />);
    const current = () => document.querySelector('[aria-current="true"]')?.getAttribute("data-exception");
    expect(current()).toBe("AP-AAA111");
    fireEvent.keyDown(window, { key: "j" });
    expect(current()).toBe("AP-BBB222");
    fireEvent.keyDown(window, { key: "j" });
    expect(current()).toBe("AP-BBB222");
    fireEvent.keyDown(window, { key: "k" });
    expect(current()).toBe("AP-AAA111");
    fireEvent.keyDown(window, { key: "Enter" });
    expect(routerSpies.push).toHaveBeenCalledWith(`/pay/invoices/${FIRST}`);
  });

  it("drafts a message and sends it for approval to a contact on file", async () => {
    vi.mocked(pay.draftExceptionMessage).mockResolvedValue({ ok: true, draft: draft() });
    vi.mocked(pay.sendExceptionMessage).mockResolvedValue({
      ok: true,
      message: {
        id: "44444444-4444-4444-8444-444444444444",
        reference: "MSG-ABC123",
        invoice_id: FIRST,
        category: "missing_receipt",
        template_id: "delivery_note",
        recipient_kind: "vendor",
        recipient_name: "Amal",
        recipient_email: "amal@vendor.example",
        message: "Please send the delivery note.",
        locale: "en",
        ai_note: true,
        status: "awaiting_approval",
        status_reason: null,
        approval_id: "55555555-5555-4555-8555-555555555555",
        created_at: "2026-10-14T08:00:00Z",
        sent_at: null,
      },
    });
    renderWithIntl(<ExceptionsQueue data={data()} locale="en" initialCategory={null} />);
    await userEvent.click(screen.getByRole("button", { name: "Write a message" }));
    await userEvent.click(screen.getByLabelText("Add a personal paragraph (AI)"));
    await userEvent.click(screen.getByRole("button", { name: "Draft" }));
    await waitFor(() => expect(screen.getByLabelText("Text")).toHaveValue(draft().message));
    expect(vi.mocked(pay.draftExceptionMessage)).toHaveBeenCalledWith(FIRST, {
      category: "missing_receipt",
      template_id: "delivery_note",
      locale: "en",
      personalise: true,
    });
    expect(screen.getByText(/written by AI/)).toBeTruthy();
    await userEvent.click(screen.getByRole("button", { name: "Send for approval" }));
    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("MSG-ABC123 is waiting for approval"));
    expect(vi.mocked(pay.sendExceptionMessage)).toHaveBeenCalledWith(FIRST, {
      category: "missing_receipt",
      template_id: "delivery_note",
      locale: "en",
      message: draft().message,
      recipient_email: "amal@vendor.example",
      ai_note: true,
      model_call_id: draft().model_call_id,
    });
  });

  it("reads in Arabic and hides messages without the permission", () => {
    renderWithIntl(<ExceptionsQueue data={data({ can_message: false })} locale="ar" initialCategory="missing_receipt" />, "ar");
    expect(screen.getByText("لا يوجد إيصال استلام مسجل.")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "كتابة رسالة" })).toBeNull();
    expect(document.querySelector('[data-exception="AP-BBB222"]')).toBeNull();
  });

  it("shows the empty state", () => {
    renderWithIntl(<ExceptionsQueue data={data({ items: [], total: 0, overdue: 0 })} locale="en" initialCategory={null} />);
    expect(screen.getByText("Nothing waiting")).toBeTruthy();
  });
});
