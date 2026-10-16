// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { HireOffer, HireOfferList, HireOfferTemplate, HirePortalOffer } from "@/lib/hire/types";

import { renderWithIntl } from "../../../test/intl";
import { navigationMock } from "../../../test/navigation-mock";

vi.mock("@/i18n/navigation", () => navigationMock);
vi.mock("@/lib/actions/offers", () => ({ createOffer: vi.fn(), offerAction: vi.fn() }));
vi.mock("@/lib/actions/portal-offer", () => ({ answerOffer: vi.fn() }));

const staff = await import("@/lib/actions/offers");
const portal = await import("@/lib/actions/portal-offer");
const { OfferBuilder } = await import("../hire/offer-builder");
const { OfferView } = await import("../portal/offer-view");

const APP = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const COMP = {
  currency: "AED",
  base_monthly: "20000.00",
  housing_monthly: "5000.00",
  transport_monthly: "1000.00",
  other_monthly: "0.00",
  bonus_target_pct: "10.00",
  annual_total: "312000.00",
};
const TEMPLATES: HireOfferTemplate[] = [
  {
    key: "standard",
    name: "Full time, permanent",
    name_ar: "دوام كامل، عقد دائم",
    employment_type: "full_time",
    conditions: ["Probation period of 6 months."],
    conditions_ar: ["فترة تجربة مدتها 6 أشهر."],
  },
];

function offer(patch: Partial<HireOffer> = {}): HireOffer {
  return {
    id: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
    application_id: APP,
    reference: "OFR-ABC234",
    template: "standard",
    locale: "en",
    job_title: "Backend Engineer",
    start_date: "2026-12-01",
    conditions: ["Probation period of 6 months."],
    compensation: COMP,
    status: "pending_approval",
    status_reason: null,
    sent: false,
    version: 1,
    approval_id: "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
    chain: "standard",
    steps: [
      { name: "hiring_manager", status: "approved" },
      { name: "hr", status: "active" },
    ],
    message_id: null,
    created_by: "dddddddd-dddd-4ddd-8ddd-dddddddddddd",
    submitted_by: "dddddddd-dddd-4ddd-8ddd-dddddddddddd",
    approved_at: null,
    responded_at: null,
    response_record_id: null,
    created_at: "2026-10-13T08:00:00Z",
    can_edit: false,
    can_send: false,
    can_withdraw: true,
    ...patch,
  };
}

function list(patch: Partial<HireOfferList> = {}): HireOfferList {
  return { application_id: APP, can_write: true, sees_compensation: true, items: [offer()], ...patch };
}

describe("OfferBuilder", () => {
  it("shows the approval chain and the compensation to people allowed to see it", () => {
    renderWithIntl(<OfferBuilder initial={list()} templates={TEMPLATES} defaultTitle="" />);
    expect(screen.getByText("Waiting for approval")).toBeInTheDocument();
    expect(screen.getByText("Hiring manager: Approved")).toBeInTheDocument();
    expect(screen.getByText("HR: Now")).toBeInTheDocument();
    expect(screen.getByText("AED 312,000.00")).toBeInTheDocument();
    // One open offer at a time: no new offer form.
    expect(screen.queryByRole("button", { name: "Create the draft" })).not.toBeInTheDocument();
  });

  it("hides compensation from readers without the permission", () => {
    renderWithIntl(
      <OfferBuilder
        initial={list({ can_write: false, sees_compensation: false, items: [offer({ compensation: null, can_withdraw: false })] })}
        templates={TEMPLATES}
        defaultTitle=""
      />,
    );
    expect(screen.getByText(/visible only to people allowed/)).toBeInTheDocument();
    expect(screen.queryByText(/312,000/)).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /Letter/ })).not.toBeInTheDocument();
  });

  it("builds a draft from a template with the annual total", async () => {
    const user = userEvent.setup();
    vi.mocked(staff.createOffer).mockResolvedValue({ ok: true, offer: offer({ status: "draft", can_edit: true, steps: [], chain: null }) });
    renderWithIntl(<OfferBuilder initial={list({ items: [] })} templates={TEMPLATES} defaultTitle="" />);
    await user.type(screen.getByLabelText("Start date"), "2026-12-01");
    await user.type(screen.getByLabelText("Basic salary (monthly)"), "20000");
    expect(screen.getByText("Annual total: AED 240,000.00")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Create the draft" }));
    expect(staff.createOffer).toHaveBeenCalledWith(
      expect.objectContaining({
        applicationId: APP,
        template: "standard",
        startDate: "2026-12-01",
        conditions: ["Probation period of 6 months."],
        compensation: expect.objectContaining({ base_monthly: "20000", currency: "AED" }),
      }),
    );
    await waitFor(() => expect(screen.getByRole("button", { name: "Submit for approval" })).toBeInTheDocument());
  });
});

function portalOffer(patch: Partial<HirePortalOffer> = {}): HirePortalOffer {
  return {
    id: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
    reference: "OFR-ABC234",
    company: "Acme Logistics",
    job_title: "Backend Engineer",
    start_date: "2026-12-01",
    compensation: COMP,
    conditions: ["Probation period of 6 months."],
    template_name: "Full time, permanent",
    status: "sent",
    responded_at: null,
    ...patch,
  };
}

describe("OfferView", () => {
  it("accepts only after the candidate confirms, once", async () => {
    const user = userEvent.setup();
    vi.mocked(portal.answerOffer).mockResolvedValue({ ok: true, offer: portalOffer({ status: "accepted" }) });
    renderWithIntl(<OfferView initial={portalOffer()} />);
    const accept = screen.getByRole("button", { name: "Accept the offer" });
    expect(accept).toBeDisabled();
    await user.click(screen.getByLabelText(/I have read the offer/));
    await user.click(accept);
    expect(portal.answerOffer).toHaveBeenCalledWith({ offerId: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb", accept: true, note: "" });
    await waitFor(() => expect(screen.getByText(/You accepted this offer/)).toBeInTheDocument());
    expect(screen.queryByRole("button", { name: "Decline the offer" })).not.toBeInTheDocument();
  });

  it("reads in Arabic", () => {
    renderWithIntl(<OfferView initial={portalOffer()} />, "ar");
    expect(screen.getByRole("button", { name: "قبول العرض" })).toBeInTheDocument();
  });
});
