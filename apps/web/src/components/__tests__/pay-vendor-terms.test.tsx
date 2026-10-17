// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { PayVendorDetail } from "@/lib/pay/types";

import { renderWithIntl } from "../../../test/intl";
import { navigationMock, routerSpies } from "../../../test/navigation-mock";

vi.mock("@/i18n/navigation", () => navigationMock);
vi.mock("next/navigation", () => ({ useRouter: () => routerSpies }));
vi.mock("@/lib/actions/pay", () => ({
  requestBankChange: vi.fn(),
  updatePaymentTerms: vi.fn(),
  vendorAction: vi.fn(),
}));

const pay = await import("@/lib/actions/pay");
const { PaymentTermsPanel } = await import("../pay/vendor-detail");

const VENDOR_ID = "33333333-3333-4333-8333-333333333333";

function vendor(patch: Partial<PayVendorDetail> = {}): PayVendorDetail {
  return {
    id: VENDOR_ID,
    reference: "VEN-4M8R2T",
    legal_name: "Seltus IT Solutions LLC (synthetic)",
    status: "active",
    payment_terms: "Net 30",
    can_write: true,
    ...patch,
  } as PayVendorDetail;
}

describe("PaymentTermsPanel", () => {
  it("shows the terms and saves new ones through the vendor update", async () => {
    const onSaved = vi.fn();
    vi.mocked(pay.updatePaymentTerms).mockResolvedValue({ ok: true, vendor: vendor({ payment_terms: "2/10 net 30" }) });
    renderWithIntl(<PaymentTermsPanel vendor={vendor()} onSaved={onSaved} />);
    expect(screen.getByText("Net 30")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Edit terms" }));
    const input = screen.getByLabelText("Agreed terms");
    await userEvent.clear(input);
    await userEvent.type(input, "2/10 net 30");
    await userEvent.click(screen.getByRole("button", { name: "Save terms" }));
    await waitFor(() => expect(onSaved).toHaveBeenCalled());
    expect(pay.updatePaymentTerms).toHaveBeenCalledWith(VENDOR_ID, "2/10 net 30");
    expect(onSaved.mock.calls[0]![0].payment_terms).toBe("2/10 net 30");
    expect(screen.queryByLabelText("Agreed terms")).not.toBeInTheDocument();
  });

  it("clears the terms with an empty value and shows a refusal", async () => {
    vi.mocked(pay.updatePaymentTerms).mockResolvedValue({ ok: false, code: "forbidden" });
    renderWithIntl(<PaymentTermsPanel vendor={vendor()} onSaved={vi.fn()} />);
    await userEvent.click(screen.getByRole("button", { name: "Edit terms" }));
    await userEvent.clear(screen.getByLabelText("Agreed terms"));
    await userEvent.click(screen.getByRole("button", { name: "Save terms" }));
    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expect(pay.updatePaymentTerms).toHaveBeenLastCalledWith(VENDOR_ID, "");
  });

  it("is read only without write access or for an archived vendor", () => {
    const { unmount } = renderWithIntl(<PaymentTermsPanel vendor={vendor({ can_write: false })} onSaved={vi.fn()} />);
    expect(screen.queryByRole("button", { name: "Edit terms" })).not.toBeInTheDocument();
    unmount();
    renderWithIntl(<PaymentTermsPanel vendor={vendor({ status: "archived", payment_terms: null })} onSaved={vi.fn()} />);
    expect(screen.queryByRole("button", { name: "Edit terms" })).not.toBeInTheDocument();
    expect(screen.getByText("No terms on file")).toBeInTheDocument();
  });

  it("renders in Arabic", () => {
    renderWithIntl(<PaymentTermsPanel vendor={vendor()} onSaved={vi.fn()} />, "ar");
    expect(screen.getByText("شروط الدفع")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "تعديل الشروط" })).toBeInTheDocument();
  });
});
