// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { PayOnboardingPage, PayTicketDetail, PayVendorCheck } from "@/lib/pay/types";

import { renderWithIntl } from "../../../test/intl";
import { navigationMock, routerSpies } from "../../../test/navigation-mock";

vi.mock("@/i18n/navigation", () => navigationMock);
vi.mock("next/navigation", () => ({ useRouter: () => routerSpies }));
vi.mock("@/lib/auth/client", () => ({ authClient: { signOut: vi.fn() } }));
vi.mock("@/lib/actions/pay", () => ({
  checkVendor: vi.fn(),
  createVendor: vi.fn(),
  requestBankChange: vi.fn(),
  recordCall: vi.fn(),
  startApproval: vi.fn(),
  cancelBankChange: vi.fn(),
  vendorAction: vi.fn(),
  createInvite: vi.fn(),
  revokeInvite: vi.fn(),
}));
vi.mock("@/lib/actions/vendor-onboarding", () => ({
  openVendorLink: vi.fn(),
  loadVendorForm: vi.fn(),
  submitVendorForm: vi.fn(),
}));

const pay = await import("@/lib/actions/pay");
const { BankChangeView, ChangeProgress } = await import("../pay/bank-change");
const { VendorForm } = await import("../pay/vendors");
const { VendorOnboardingForm } = await import("../pay/vendor-onboarding");

const ON_FILE = "+97128611629";
const NEW_IBAN = "AE070331234567890123456";

function account(patch: Partial<PayTicketDetail["proposed_account"]> = {}): PayTicketDetail["proposed_account"] {
  return {
    id: "11111111-1111-4111-8111-111111111111",
    status: "proposed",
    iban_masked: "AE** **** 3456",
    country: "AE",
    bank_code: "033",
    bank_name: "Noora National Bank (synthetic)",
    swift: "XQNNAEAD",
    account_name: "Seltus IT Solutions LLC",
    created_at: "2026-10-14T08:00:00Z",
    activated_at: null,
    retired_at: null,
    ...patch,
  };
}

function ticket(patch: Partial<PayTicketDetail> = {}, verification: Partial<PayTicketDetail["verification"]> = {}): PayTicketDetail {
  return {
    id: "22222222-2222-4222-8222-222222222222",
    reference: "BCT-7K2QX9",
    vendor_id: "33333333-3333-4333-8333-333333333333",
    vendor_name: "Seltus IT Solutions LLC",
    vendor_reference: "VEN-4M8R2T",
    status: "awaiting_verification",
    source: "email",
    new_iban_masked: "AE** **** 3456",
    created_at: "2026-10-14T08:00:00Z",
    updated_at: "2026-10-14T08:00:00Z",
    activates_at: null,
    outcome: null,
    requested_by_type: "user",
    requested_by_id: "44444444-4444-4444-8444-444444444444",
    requester_user_id: "44444444-4444-4444-8444-444444444444",
    note: "Email from accounts asking to pay a new account.",
    request_had_contact_details: true,
    previous_account: account({ id: "55555555-5555-4555-8555-555555555555", status: "active", iban_masked: "AE** **** 1715", bank_code: "917" }),
    proposed_account: account(),
    proposed_iban: NEW_IBAN,
    verification: {
      phone_on_file: ON_FILE,
      phone_last4: "1629",
      phone_recently_changed: false,
      verified_by: null,
      verified_at: null,
      calls: [],
      ...verification,
    },
    approval_id: null,
    approval_status: null,
    approval_steps: [],
    approver_ids: [],
    approved_at: null,
    cooling_off_hours: 24,
    completed_at: null,
    closed_at: null,
    hold_kept: null,
    vendor_payment_hold: true,
    vendor_hold_reason: "bank_change",
    decision_ids: [],
    can_verify: true,
    can_cancel: true,
    can_start_approval: false,
    ...patch,
  };
}

describe("BankChangeView", () => {
  it("shows the verifier the number on file only, with the call script and the hold", () => {
    renderWithIntl(<BankChangeView initial={ticket()} totpEnabled returnPath="/en/pay/bank-changes/x" />);
    const link = screen.getByText(ON_FILE).closest("a");
    expect(link).toHaveAttribute("href", `tel:${ON_FILE}`);
    expect(document.querySelectorAll("a[href^='tel:']")).toHaveLength(1);
    expect(screen.getByText("Pairly never shows a number from the request.")).toBeInTheDocument();
    expect(document.querySelector("[data-request-had-contact]")).not.toBeNull();
    // The new IBAN to read back, grouped in fours.
    expect(screen.getAllByText("AE07 0331 2345 6789 0123 456").length).toBeGreaterThan(0);
    expect(screen.getByText(/Payments on hold/)).toBeInTheDocument();
    expect(document.querySelector("[data-step='call']")).toHaveAttribute("data-state", "current");
  });

  it("records a call only with a result and the attestation, and never sends a number", async () => {
    const user = userEvent.setup();
    vi.mocked(pay.recordCall).mockResolvedValue({
      ok: true,
      ticket: ticket({ status: "awaiting_approval", approval_id: "66666666-6666-4666-8666-666666666666", can_verify: false }),
    });
    renderWithIntl(<BankChangeView initial={ticket()} totpEnabled returnPath="/en/pay/bank-changes/x" />);
    const record = screen.getByRole("button", { name: "Record the call" });
    expect(record).toBeDisabled();
    await user.click(screen.getByRole("radio", { name: "Confirmed the change" }));
    expect(record).toBeDisabled();
    await user.click(screen.getByLabelText(/I called the number on file/));
    await user.type(screen.getByLabelText("Notes from the call"), "Spoke to Fatima.");
    await user.click(record);
    await waitFor(() => expect(pay.recordCall).toHaveBeenCalledTimes(1));
    expect(pay.recordCall).toHaveBeenCalledWith("22222222-2222-4222-8222-222222222222", {
      result: "confirmed",
      notes: "Spoke to Fatima.",
      called_number_on_file: true,
    });
    expect(await screen.findByRole("link", { name: "Open in the inbox" })).toHaveAttribute(
      "href",
      "/inbox/66666666-6666-4666-8666-666666666666",
    );
  });

  it("hides the number from people who do not verify, and the form from the requester", () => {
    renderWithIntl(
      <BankChangeView initial={ticket({ can_verify: false, proposed_iban: null }, { phone_on_file: null })} totpEnabled returnPath="/x" />,
    );
    expect(screen.queryByText(ON_FILE)).toBeNull();
    expect(document.querySelectorAll("a[href^='tel:']")).toHaveLength(0);
    expect(screen.getByText(/It ends in 1629/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Record the call" })).toBeNull();
    expect(screen.getByText("A person who verifies bank changes makes the call.")).toBeInTheDocument();
  });

  it("tells the requester someone else must call", () => {
    renderWithIntl(<BankChangeView initial={ticket({ can_verify: false })} totpEnabled returnPath="/x" />);
    expect(screen.getByText("You asked for this change, so someone else must make the call.")).toBeInTheDocument();
  });

  it("warns when the number on file changed recently", () => {
    renderWithIntl(<BankChangeView initial={ticket({}, { phone_recently_changed: true })} totpEnabled returnPath="/x" />);
    expect(document.querySelector("[data-phone-recently-changed]")).not.toBeNull();
  });

  it("shows the cooling-off period and keeps the hold by default when cancelling then", () => {
    renderWithIntl(
      <BankChangeView
        initial={ticket({
          status: "cooling_off",
          activates_at: "2026-10-15T08:00:00Z",
          approval_id: "66666666-6666-4666-8666-666666666666",
          approval_steps: [
            { name: "first_approval", status: "approved", decided_by: [] },
            { name: "second_approval", status: "approved", decided_by: [] },
          ],
          can_verify: false,
        })}
        totpEnabled
        returnPath="/x"
      />,
    );
    expect(document.querySelector("[data-cooling-off]")?.textContent).toContain("24 hours");
    expect(screen.queryByRole("button", { name: "Record the call" })).toBeNull();
    expect(screen.getByLabelText(/Keep payments on hold/)).toBeChecked();
  });

  it("explains a failed call and that the hold stays", () => {
    renderWithIntl(
      <BankChangeView
        initial={ticket({ status: "rejected", outcome: "verification_failed", hold_kept: true, can_verify: false, can_cancel: false, vendor_hold_reason: "verification_failed" })}
        totpEnabled
        returnPath="/x"
      />,
    );
    expect(screen.getByText(/did not confirm the change on the number on file/)).toBeInTheDocument();
    expect(screen.getByText(/stay on hold until finance releases them/)).toBeInTheDocument();
  });
});

describe("ChangeProgress", () => {
  it("marks every step done once the change is active", () => {
    renderWithIntl(<ChangeProgress status="completed" />);
    const states = [...document.querySelectorAll("[data-step]")].map((el) => el.getAttribute("data-state"));
    expect(states).toEqual(["done", "done", "done", "done", "done"]);
  });
});

describe("VendorForm", () => {
  it("shows duplicate hints and asks to acknowledge a similar name only", async () => {
    const user = userEvent.setup();
    const check: PayVendorCheck = {
      duplicates: [
        { vendor_id: "77777777-7777-4777-8777-777777777777", reference: "VEN-AAAAAA", legal_name: "Seltus IT Solutions LLC", status: "active", reasons: ["name"], score: "0.97" },
      ],
      trn: { valid: true, error: null, country: null, bank_code: null, formatted: "198427401849981" },
      iban: { valid: true, error: null, country: "AE", bank_code: "033", formatted: "AE07 0331 2345 6789 0123 456" },
      swift: null,
      warnings: [],
    };
    vi.mocked(pay.checkVendor).mockResolvedValue({ ok: true, check });
    vi.mocked(pay.createVendor).mockResolvedValue({ ok: true, vendor: { id: "88888888-8888-4888-8888-888888888888" } as never });
    const created = vi.fn();
    renderWithIntl(<VendorForm onCreated={created} />);
    await user.type(screen.getByLabelText("Legal name"), "Seltus I.T. Solutions");
    await user.type(screen.getByLabelText("Phone number on file"), "+971 2 861 1629");
    await user.type(screen.getByLabelText("IBAN"), NEW_IBAN);
    await user.click(screen.getByRole("button", { name: "Check details" }));
    expect(await screen.findByText(/: similar name$/)).toBeInTheDocument();
    expect(screen.getByText("IBAN AE07 0331 2345 6789 0123 456 is valid (bank code 033).")).toBeInTheDocument();
    await user.click(screen.getByLabelText("I checked the similar names and this is a different company."));
    await user.click(screen.getByRole("button", { name: "Create vendor" }));
    await waitFor(() => expect(created).toHaveBeenCalledWith("88888888-8888-4888-8888-888888888888"));
    const [input, acknowledged] = vi.mocked(pay.createVendor).mock.calls[0]!;
    expect(acknowledged).toBe(true);
    expect(input.bank?.iban).toBe(NEW_IBAN);
  });

  it("does not offer to override the same TRN or IBAN", async () => {
    const user = userEvent.setup();
    vi.mocked(pay.checkVendor).mockResolvedValue({
      ok: true,
      check: {
        duplicates: [{ vendor_id: "77777777-7777-4777-8777-777777777777", reference: "VEN-AAAAAA", legal_name: "Other", status: "active", reasons: ["iban"], score: "1.0" }],
        trn: null,
        iban: null,
        swift: null,
        warnings: [],
      },
    });
    renderWithIntl(<VendorForm onCreated={vi.fn()} />);
    await user.type(screen.getByLabelText("Legal name"), "Other Trading");
    await user.click(screen.getByRole("button", { name: "Check details" }));
    expect(await screen.findByText(/same IBAN/)).toBeInTheDocument();
    expect(document.querySelector("[data-acknowledge-similar]")).toBeNull();
  });
});

describe("VendorOnboardingForm", () => {
  const page: PayOnboardingPage = {
    company: "Acme Trading",
    company_hint: "Seltus",
    locale: "en",
    documents: [],
    max_documents: 10,
    expires_at: "2026-10-15T08:00:00Z",
  };

  it("sends the form only after the confirmation, then thanks the vendor", async () => {
    const user = userEvent.setup();
    const submit = vi.fn().mockResolvedValue({ ok: true, reference: "VEN-ZZZZZZ" });
    const upload = vi.fn();
    renderWithIntl(<VendorOnboardingForm initial={page} actions={{ submit, upload }} />);
    expect(screen.getByRole("heading", { name: "Your details for Acme Trading" })).toBeInTheDocument();
    expect(screen.getByLabelText(/Legal name/, { selector: "input[required]" })).toHaveValue("Seltus");
    const send = screen.getByRole("button", { name: "Send my details" });
    expect(send).toBeDisabled();
    await user.type(screen.getByLabelText(/Main company phone number/), "+971 2 861 1629");
    await user.type(screen.getByLabelText(/Your name/), "Fatima Nasser");
    await user.type(screen.getByLabelText(/^IBAN/), NEW_IBAN);
    await user.type(screen.getByLabelText(/Name on the account/), "Seltus IT Solutions LLC");
    await user.click(screen.getByLabelText(/I confirm these details are correct/));
    await user.click(send);
    await waitFor(() => expect(submit).toHaveBeenCalledTimes(1));
    const sent = submit.mock.calls[0]![0];
    expect(sent.bank.iban).toBe(NEW_IBAN);
    expect(sent.phone_on_file).toBe("+971 2 861 1629");
    expect(await screen.findByText(/reference VEN-ZZZZZZ/)).toBeInTheDocument();
  });

  it("shows the API's reason when the bank details are refused", async () => {
    const user = userEvent.setup();
    const submit = vi.fn().mockResolvedValue({ ok: false, code: "bank_details_invalid" });
    const { container } = renderWithIntl(<VendorOnboardingForm initial={page} actions={{ submit, upload: vi.fn() }} />, "ar");
    for (const input of container.querySelectorAll<HTMLInputElement>("input[required]:not([type='checkbox'])")) {
      if (!input.value) await user.type(input, input.type === "tel" ? "+971 2 861 1629" : "Seltus");
    }
    await user.click(screen.getByRole("checkbox"));
    await user.click(screen.getByRole("button", { name: "إرسال بياناتي" }));
    expect(await screen.findByRole("alert")).toHaveAttribute("data-error", "bank_details_invalid");
  });
});
