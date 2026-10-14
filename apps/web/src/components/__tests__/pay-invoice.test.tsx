// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { PayInvoiceDetail, PayInvoiceField } from "@/lib/pay/types";

import { renderWithIntl } from "../../../test/intl";
import { navigationMock, routerSpies } from "../../../test/navigation-mock";

vi.mock("@/i18n/navigation", () => navigationMock);
vi.mock("next/navigation", () => ({ useRouter: () => routerSpies }));
vi.mock("@/lib/actions/pay", () => ({ correctInvoiceField: vi.fn(), invoiceAction: vi.fn() }));
vi.mock("@/lib/actions/documents", () => ({ documentSource: vi.fn() }));

const pay = await import("@/lib/actions/pay");
const { InvoiceReview } = await import("../pay/invoice-review");
const { InvoiceList } = await import("../pay/invoices");

const TEXT = "Invoice No: INV-7\nTotal: AED 1,050.00\n1. Toner 2 500.00 5.00 1000.00";

function f(path: string, value: string | null, extra: Partial<PayInvoiceField> = {}): PayInvoiceField {
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

function invoice(patch: Partial<PayInvoiceDetail> = {}): PayInvoiceDetail {
  return {
    id: "33333333-3333-4333-8333-333333333333",
    reference: "AP-7K2QX9",
    checks_state: "current",
    status: "needs_review",
    source: "email",
    document_id: "44444444-4444-4444-8444-444444444444",
    vendor: null,
    vendor_name: null,
    invoice_number: "INV-7",
    invoice_date: null,
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
    po_number: null,
    subtotal: null,
    vat_amount: null,
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
        f("total", "1,050.00", { needs_correction: true, flags: ["low_confidence"], confidence: 0 }),
        f("invoice_number", "INV-7"),
        f("line_items[0].description", "Toner"),
        f("line_items[0].line_total", "1000.00"),
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
    ...patch,
  };
}

describe("InvoiceReview", () => {
  it("marks doubtful fields, starts there and shows the source in the text", async () => {
    renderWithIntl(<InvoiceReview initial={invoice()} vendors={[]} />);
    expect(screen.getByText("1 field to check")).toBeTruthy();
    const total = document.querySelector('[data-field="total"]');
    expect(total?.getAttribute("data-needs-look")).toBe("true");
    expect(within(total as HTMLElement).getByText("Check this")).toBeTruthy();
    expect(screen.getByText("No vendor matched")).toBeTruthy();
    // The first doubtful field is where review starts: it is the one marked in the text.
    const marked = document.querySelector('[data-source~="total"]');
    expect(marked?.className).toContain("ring-1");
    // Click another field: its source is marked instead.
    await userEvent.click(within(document.querySelector('[data-field="invoice_number"]') as HTMLElement).getAllByRole("button")[0]!);
    expect(document.querySelector('[data-source~="invoice_number"]')?.className).toContain("ring-1");
    expect(document.querySelector('[data-source~="total"]')?.className).not.toContain("ring-1");
  });

  it("moves between fields with the keyboard and opens the editor with e", async () => {
    renderWithIntl(<InvoiceReview initial={invoice()} vendors={[]} />);
    const first = within(document.querySelector('[data-field="invoice_number"]') as HTMLElement).getAllByRole("button")[0]!;
    first.focus();
    await userEvent.keyboard("j");
    expect(document.activeElement?.closest("[data-field]")?.getAttribute("data-field")).toBe("total");
    await userEvent.keyboard("{ArrowDown}");
    expect(document.activeElement?.closest("[data-field]")?.getAttribute("data-field")).toBe(
      "line_items[0].description",
    );
    await userEvent.keyboard("e");
    expect(document.querySelector("[data-edit-form]")).toBeTruthy();
    await userEvent.keyboard("{Escape}");
    expect(document.querySelector("[data-edit-form]")).toBeNull();
  });

  it("records a correction with a reason", async () => {
    const corrected = invoice({
      extraction: {
        ...invoice().extraction!,
        fields: [f("total", "1050.00", { corrected: true }), f("invoice_number", "INV-7")],
      },
    });
    vi.mocked(pay.correctInvoiceField).mockResolvedValue({ ok: true, invoice: corrected } as never);
    renderWithIntl(<InvoiceReview initial={invoice()} vendors={[]} />);
    const total = document.querySelector('[data-field="total"]') as HTMLElement;
    await userEvent.click(within(total).getByRole("button", { name: "Correct this field" }));
    const value = screen.getByLabelText("Correct value");
    await userEvent.clear(value);
    await userEvent.type(value, "1050.00");
    const save = screen.getByRole("button", { name: "Save correction" });
    expect((save as HTMLButtonElement).disabled).toBe(true);
    await userEvent.type(screen.getByLabelText("Why (required)"), "Comma read as a decimal point");
    await userEvent.click(save);
    await waitFor(() =>
      expect(pay.correctInvoiceField).toHaveBeenCalledWith(invoice().id, {
        field_path: "total",
        new_value: "1050.00",
        reason: "Comma read as a decimal point",
      }),
    );
    await waitFor(() => expect(screen.getByText("Nothing left to check.")).toBeTruthy());
  });

  it("hides the decisions from people who only read", () => {
    renderWithIntl(<InvoiceReview initial={invoice({ can_process: false })} vendors={[]} />);
    expect(screen.queryByRole("button", { name: "Mark as reviewed" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Correct this field" })).toBeNull();
  });

  it("shows the guardrail notice, the checks and a suspected original on a text invoice", () => {
    renderWithIntl(
      <InvoiceReview
        initial={invoice({
          findings: [
            {
              id: "f1",
              type: "instruction",
              code: "approval_manipulation",
              severity: "high",
              location: "body",
              page: 1,
              bbox: null,
              remove: true,
              evidence: "approve this invoice",
            },
          ],
          checks: [
            {
              check: "injection",
              status: "fail",
              error: null,
              findings: [
                {
                  code: "prompt_injection",
                  rule: "prompt_injection",
                  severity: "critical",
                  message: { en: "The document contains text that tries to instruct an AI.", ar: "نص" },
                  evidence: {},
                  original_id: null,
                  related_ids: [],
                  needs_review: true,
                },
              ],
            },
          ],
          risk: null,
          compare: [],
        })}
        vendors={[]}
      />,
    );
    expect(document.querySelector("[data-guardrail-notice]")).toBeTruthy();
    expect(document.querySelector('[data-finding="prompt_injection"]')).toBeTruthy();
    expect(screen.getByText("The document contains text that tries to instruct an AI.")).toBeTruthy();
    expect(document.querySelector("[data-compare-panel]")).toBeNull();
  });

  it("reads in Arabic", () => {
    renderWithIntl(<InvoiceReview initial={invoice()} vendors={[]} />, "ar");
    expect(screen.getByText("تحتاج مراجعة")).toBeTruthy();
    expect(screen.getByText("لم يُطابق أي مورد")).toBeTruthy();
  });
});

describe("InvoiceList", () => {
  it("shows status filters with counts and links each invoice", () => {
    const { reference, id, ...rest } = invoice();
    renderWithIntl(
      <InvoiceList
        items={[{ ...rest, reference, id }]}
        counts={{ needs_review: 1, ready: 2 }}
        status="needs_review"
      />,
    );
    const filter = screen.getByRole("navigation", { name: "Filter by status" });
    expect(within(filter).getByText("All").closest("a")?.getAttribute("aria-current")).toBeNull();
    expect(within(filter).getByText("Needs review").closest("a")?.getAttribute("aria-current")).toBe("page");
    expect(screen.getByText("Unknown vendor")).toBeTruthy();
    expect(document.querySelector('[data-invoice="AP-7K2QX9"]')).toBeTruthy();
  });
});
