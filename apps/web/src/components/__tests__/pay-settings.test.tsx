// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { PaySettings } from "@/lib/pay/types";

import { renderWithIntl } from "../../../test/intl";
import { navigationMock, routerSpies } from "../../../test/navigation-mock";

vi.mock("@/i18n/navigation", () => navigationMock);
vi.mock("next/navigation", () => ({ useRouter: () => routerSpies }));
vi.mock("@/lib/actions/pay", () => ({ savePaySettings: vi.fn() }));

const pay = await import("@/lib/actions/pay");
const { PaySettingsView, formOf, inputOf } = await import("../pay/pay-settings");

function settings(patch: Partial<PaySettings> = {}): PaySettings {
  return {
    cooling_off_hours: 24,
    phone_change_window_days: 30,
    checks: {
      duplicate_window_days: 60,
      match_price_pct: "2.00",
      match_quantity_pct: "0.00",
      match_total_pct: "1.00",
      match_total_amount: "10.00",
      require_po: false,
      standard_vat_rate: "5.00",
    },
    payments: { auto_approve_low_risk: false, auto_approve_limit: "0", auto_approve_currency: "AED" },
    can_manage: true,
    ...patch,
  };
}

beforeEach(() => vi.mocked(pay.savePaySettings).mockReset());

describe("pay settings form", () => {
  it("reads the settings in force and sends only what the page shows", () => {
    const form = formOf(settings());
    expect(form.cooling_off_hours).toBe("24");
    expect(form.match_price_pct).toBe("2.00");
    expect(form.require_po).toBe(false);
    const input = inputOf({ ...form, auto_approve_currency: " aed ", auto_approve_low_risk: true });
    expect(input).toEqual({
      cooling_off_hours: 24,
      phone_change_window_days: 30,
      checks: {
        duplicate_window_days: 60,
        match_price_pct: "2.00",
        match_quantity_pct: "0.00",
        match_total_pct: "1.00",
        match_total_amount: "10.00",
        require_po: false,
      },
      payments: { auto_approve_low_risk: true, auto_approve_limit: "0", auto_approve_currency: "AED" },
    });
    expect(Number.isNaN(inputOf({ ...form, cooling_off_hours: "1.5" }).cooling_off_hours)).toBe(true);
  });

  it("saves the cooling-off, the checks and rule approval", async () => {
    vi.mocked(pay.savePaySettings).mockResolvedValue({
      ok: true,
      settings: settings({ cooling_off_hours: 48, checks: { ...settings().checks, require_po: true } }),
    });
    renderWithIntl(<PaySettingsView initial={settings()} totpEnabled />);
    const hours = screen.getByLabelText("Cooling-off (hours)");
    await userEvent.clear(hours);
    await userEvent.type(hours, "48");
    await userEvent.click(screen.getByLabelText(/Require a purchase order/));
    await userEvent.click(screen.getByLabelText(/Approve low-risk invoices by rule/));
    await userEvent.click(screen.getByRole("button", { name: "Save settings" }));
    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("Saved"));
    const sent = vi.mocked(pay.savePaySettings).mock.calls[0]?.[0];
    expect(sent?.cooling_off_hours).toBe(48);
    expect(sent?.checks.require_po).toBe(true);
    expect(sent?.payments.auto_approve_low_risk).toBe(true);
  });

  it("asks for a second factor when the API wants one", async () => {
    vi.mocked(pay.savePaySettings).mockResolvedValue({ ok: false, code: "second_factor_required" });
    renderWithIntl(<PaySettingsView initial={settings()} totpEnabled={false} />);
    await userEvent.click(screen.getByRole("button", { name: "Save settings" }));
    await waitFor(() => expect(document.querySelector("[data-pay-settings]")).toHaveTextContent(/authenticator/i));
  });

  it("is read only without the manage permission, in Arabic too", () => {
    renderWithIntl(<PaySettingsView initial={settings({ can_manage: false })} totpEnabled />, "ar");
    expect(screen.queryByRole("button", { name: "حفظ الإعدادات" })).toBeNull();
    expect(screen.getByLabelText("فترة التهدئة (بالساعات)")).toBeDisabled();
  });
});
