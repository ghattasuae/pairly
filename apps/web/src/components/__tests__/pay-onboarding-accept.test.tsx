// SPDX-License-Identifier: AGPL-3.0-only
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
  acceptOnboarding: vi.fn(),
  requestBankChange: vi.fn(),
  updatePaymentTerms: vi.fn(),
  vendorAction: vi.fn(),
}));

const pay = await import("@/lib/actions/pay");
const { AcceptOnboardingForm } = await import("../pay/vendor-detail");

const VENDOR_ID = "33333333-3333-4333-8333-333333333333";

describe("AcceptOnboardingForm (security review 2026-10, M-05)", () => {
  it("accepts only with a call-back number from an independent source and its reference", async () => {
    const accepted = { id: VENDOR_ID, status: "pending" } as PayVendorDetail;
    vi.mocked(pay.acceptOnboarding).mockResolvedValue({ ok: true, vendor: accepted });
    const onDone = vi.fn();
    renderWithIntl(<AcceptOnboardingForm vendorId={VENDOR_ID} onDone={onDone} onStepUp={vi.fn()} />);
    expect(screen.getByText(/Never use the number on the vendor's form/)).toBeInTheDocument();
    const accept = screen.getByRole("button", { name: "Accept and verify bank details" });
    expect(accept).toBeDisabled();
    await userEvent.type(screen.getByLabelText("Phone number from that source"), "+971 2 861 1630");
    await userEvent.selectOptions(screen.getByLabelText("Where it comes from"), "trade_licence");
    expect(accept).toBeDisabled(); // no reference yet
    await userEvent.type(screen.getByLabelText("Reference"), "Licence 778899, DED register");
    await userEvent.click(accept);
    await waitFor(() => expect(onDone).toHaveBeenCalledWith(accepted));
    expect(pay.acceptOnboarding).toHaveBeenCalledWith(VENDOR_ID, {
      note: "",
      callback: { phone: "+971 2 861 1630", source: "trade_licence", reference: "Licence 778899, DED register" },
    });
  });

  it("shows the API's reason when it refuses", async () => {
    vi.mocked(pay.acceptOnboarding).mockResolvedValue({ ok: false, code: "callback_source_required" });
    renderWithIntl(<AcceptOnboardingForm vendorId={VENDOR_ID} onDone={vi.fn()} onStepUp={vi.fn()} />);
    await userEvent.type(screen.getByLabelText("Phone number from that source"), "+971 2 861 1630");
    await userEvent.selectOptions(screen.getByLabelText("Where it comes from"), "official_registry");
    await userEvent.type(screen.getByLabelText("Reference"), "Registry entry 42");
    await userEvent.click(screen.getByRole("button", { name: "Accept and verify bank details" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/independent source/);
  });

  it("renders in Arabic", () => {
    renderWithIntl(<AcceptOnboardingForm vendorId={VENDOR_ID} onDone={vi.fn()} onStepUp={vi.fn()} />, "ar");
    expect(screen.getByText("رقم معاودة الاتصال")).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "الرخصة التجارية" })).toBeInTheDocument();
  });
});
