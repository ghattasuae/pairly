// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { PayInvoiceCoding, PayLineCoding } from "@/lib/pay/types";

import { renderWithIntl } from "../../../test/intl";
import { navigationMock, routerSpies } from "../../../test/navigation-mock";

vi.mock("@/i18n/navigation", () => navigationMock);
vi.mock("next/navigation", () => ({ useRouter: () => routerSpies }));
vi.mock("@/lib/actions/pay", () => ({ codeInvoiceLines: vi.fn(), askModelCoding: vi.fn() }));

const pay = await import("@/lib/actions/pay");
const { InvoiceCoding } = await import("../pay/invoice-coding");

const INVOICE = "11111111-1111-4111-8111-111111111111";

function line(index: number, patch: Partial<PayLineCoding> = {}): PayLineCoding {
  return {
    line_index: index,
    description: ["Toner cartridge, black", "Ergonomic office chair"][index] ?? "Line",
    gl_code: null,
    cost_centre: null,
    source: null,
    corrected: false,
    coded_at: null,
    suggestion: null,
    ...patch,
  };
}

function coding(patch: Partial<PayInvoiceCoding> = {}): PayInvoiceCoding {
  return {
    invoice_id: INVOICE,
    status: "needs_review",
    gl_list_version: 1,
    cc_list_version: 1,
    gl_accounts: [
      { code: "6100", name: "Office supplies", name_ar: "اللوازم المكتبية" },
      { code: "1500", name: "IT equipment", name_ar: null },
    ],
    cost_centres: [{ code: "CC-ADM", name: "Administration", name_ar: "الإدارة" }],
    lines: [
      line(0, {
        suggestion: {
          gl_code: "6100",
          cost_centre: "CC-ADM",
          confidence: 0.92,
          level: "high",
          method: "history",
          support: 2,
          examples: ["AP-1"],
          reason: null,
        },
      }),
      line(1),
    ],
    can_code: true,
    auto_coding: false,
    checks_state: "current",
    ...patch,
  };
}

beforeEach(() => {
  vi.mocked(pay.codeInvoiceLines).mockReset();
  vi.mocked(pay.askModelCoding).mockReset();
});

describe("InvoiceCoding", () => {
  it("shows each line with its suggestion and confidence, nothing applied", () => {
    renderWithIntl(<InvoiceCoding initial={coding()} locale="en" />);
    const row = document.querySelector('[data-coding-line="0"]');
    expect(row?.textContent).toContain("High 92%");
    expect(row?.textContent).toContain("Backed by 2 earlier lines");
    expect((screen.getByLabelText("GL account of line 1") as HTMLSelectElement).value).toBe("");
    expect(screen.getByRole("button", { name: "Save coding" })).toBeDisabled();
  });

  it("uses a suggestion only when asked and saves the changed lines", async () => {
    vi.mocked(pay.codeInvoiceLines).mockResolvedValue({
      ok: true,
      coding: coding({ lines: [line(0, { gl_code: "6100", cost_centre: "CC-ADM", source: "person" }), line(1)] }),
    });
    renderWithIntl(<InvoiceCoding initial={coding()} locale="en" />);
    await userEvent.click(screen.getByRole("button", { name: "Use high confidence suggestions" }));
    await userEvent.selectOptions(screen.getByLabelText("GL account of line 2"), "1500");
    await userEvent.click(screen.getByRole("button", { name: "Save 2 lines" }));
    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("The checks ran again"));
    expect(vi.mocked(pay.codeInvoiceLines)).toHaveBeenCalledWith(INVOICE, [
      { line_index: 0, gl_code: "6100", cost_centre: "CC-ADM" },
      { line_index: 1, gl_code: "1500", cost_centre: null },
    ]);
    expect(routerSpies.refresh).toHaveBeenCalled();
  });

  it("shows a model suggestion as such, and says when the model is unavailable", async () => {
    vi.mocked(pay.askModelCoding).mockResolvedValue({
      ok: true,
      lines: [
        {
          line_index: 1,
          suggestion: {
            gl_code: "1500",
            cost_centre: null,
            confidence: 0.6,
            level: "medium",
            method: "model",
            support: 0,
            examples: [],
            reason: "A chair is equipment.",
          },
          problem: null,
          from_recording: true,
        },
      ],
    });
    renderWithIntl(<InvoiceCoding initial={coding()} locale="en" />);
    await userEvent.click(screen.getByRole("button", { name: "Ask the model" }));
    await waitFor(() => expect(document.querySelector('[data-suggestion="model"]')).not.toBeNull());
    expect(screen.getByText("Model suggestion. Check it before you use it.")).toBeTruthy();
    expect(vi.mocked(pay.askModelCoding)).toHaveBeenCalledWith(INVOICE, [1]);
    expect((screen.getByLabelText("GL account of line 2") as HTMLSelectElement).value).toBe("");
  });

  it("is read only without the permission and reads in Arabic", () => {
    renderWithIntl(<InvoiceCoding initial={coding({ can_code: false })} locale="ar" />, "ar");
    expect(screen.getByText("ترميز الحسابات")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "اسأل النموذج" })).toBeNull();
    expect(screen.getByLabelText("حساب البند 1")).toBeDisabled();
  });

  it("explains a missing chart of accounts", () => {
    renderWithIntl(<InvoiceCoding initial={coding({ gl_accounts: [], cost_centres: [] })} locale="en" />);
    expect(document.querySelector("[data-coding-no-list]")).not.toBeNull();
  });
});
