// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { CalendarIntegrations, OrgIntegrations } from "@/lib/hire/scheduling";

import { renderWithIntl } from "../../../test/intl";
import { navigationMock } from "../../../test/navigation-mock";

vi.mock("@/i18n/navigation", () => navigationMock);
vi.mock("@/lib/actions/scheduling", () => ({}));

const { CalendarSettings } = await import("../settings/calendar-settings");
const { CalendarOrgSettings } = await import("../settings/calendar-org-settings");

const JOB = "44444444-4444-4444-8444-444444444444";
const APP = "55555555-5555-4555-8555-555555555555";

function personal(connected: boolean): CalendarIntegrations {
  return {
    using: connected ? "microsoft" : "fake",
    providers: [
      {
        provider: "google",
        label: "Google Calendar and Google Meet",
        availability: "needs_setup",
        flag_enabled: false,
        configured: false,
        connected: false,
        status: null,
        account_email: null,
        steps: ["An administrator creates a Google Cloud project for your organisation."],
      },
      {
        provider: "microsoft",
        label: "Microsoft 365 and Teams",
        availability: "available",
        flag_enabled: true,
        configured: true,
        connected,
        status: connected ? "active" : null,
        account_email: connected ? "rana@acme.example" : null,
        steps: [],
      },
      {
        provider: "zoom",
        label: "Zoom",
        availability: "needs_setup",
        flag_enabled: false,
        configured: false,
        connected: false,
        status: null,
        account_email: null,
        steps: [],
      },
    ],
  };
}

function org(zoomConfigured: boolean, waiting: number): OrgIntegrations {
  const base = { status: null, verified_at: null, last_error: null, steps: [] };
  return {
    operations_needing_review: waiting,
    providers: [
      { ...base, provider: "google", label: "Google", state: "needs_setup", flag_key: "hire.scheduling.google", flag_enabled: false, configured: false, connected_people: 0, missing: ["server_client", "feature_flag", "people_connect"] },
      { ...base, provider: "microsoft", label: "Microsoft", state: "connected", flag_key: "hire.scheduling.microsoft", flag_enabled: true, configured: true, connected_people: 2, missing: [] },
      {
        ...base,
        provider: "zoom",
        label: "Zoom",
        state: zoomConfigured ? "connected" : "needs_setup",
        flag_key: "hire.scheduling.zoom",
        flag_enabled: zoomConfigured,
        configured: zoomConfigured,
        connected_people: null,
        status: zoomConfigured ? "active" : null,
        verified_at: zoomConfigured ? "2026-10-14T08:00:00Z" : null,
        missing: zoomConfigured ? [] : ["zoom_app", "feature_flag"],
      },
    ],
  };
}

const noAvailability = null;

describe("CalendarSettings: Microsoft 365", () => {
  it("connects Microsoft 365 (remembering which provider) and disconnects it", async () => {
    const startConnect = vi.fn().mockResolvedValue({ ok: false, code: "unavailable" });
    const disconnectCalendar = vi.fn().mockResolvedValue({ ok: true, integrations: personal(false) });
    const view = renderWithIntl(
      <CalendarSettings
        initialIntegrations={personal(false)}
        initialAvailability={noAvailability}
        actions={{ startConnect, finishConnect: vi.fn(), disconnectCalendar, saveMyAvailability: vi.fn() }}
      />,
    );
    // Google is not set up: no button, the steps instead. Microsoft is ready.
    expect(screen.queryByRole("button", { name: "Connect Google Calendar" })).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "Connect Microsoft 365" }));
    expect(startConnect).toHaveBeenCalledWith("microsoft");
    view.unmount();

    renderWithIntl(
      <CalendarSettings
        initialIntegrations={personal(true)}
        initialAvailability={noAvailability}
        actions={{ startConnect, finishConnect: vi.fn(), disconnectCalendar, saveMyAvailability: vi.fn() }}
      />,
    );
    expect(screen.getByText("Interviews use your Microsoft 365 calendar.")).toBeInTheDocument();
    expect(screen.getByText("Connected as rana@acme.example")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Disconnect Microsoft 365 and Teams" }));
    expect(disconnectCalendar).toHaveBeenCalledWith("microsoft");
    await waitFor(() => expect(screen.getByRole("button", { name: "Connect Microsoft 365" })).toBeInTheDocument());
  });

  it("finishes the Microsoft callback once, using the provider remembered before the redirect", async () => {
    window.sessionStorage.setItem("pairly.calendar.connect", "microsoft");
    const finishConnect = vi.fn().mockResolvedValue({ ok: true, integrations: personal(true) });
    renderWithIntl(
      <CalendarSettings
        initialIntegrations={personal(false)}
        initialAvailability={noAvailability}
        callback={{ code: "0.AX-code", state: "hg1.x", hint: "google" }}
        actions={{ startConnect: vi.fn(), finishConnect, disconnectCalendar: vi.fn(), saveMyAvailability: vi.fn() }}
      />,
    );
    await waitFor(() => expect(finishConnect).toHaveBeenCalledWith("microsoft", "0.AX-code", "hg1.x"));
    expect(await screen.findByText("Your calendar is connected: Microsoft 365 and Teams.")).toBeInTheDocument();
    expect(window.sessionStorage.getItem("pairly.calendar.connect")).toBeNull();
  });

  it("reads in Arabic, with the set-up steps translated", () => {
    renderWithIntl(
      <CalendarSettings
        initialIntegrations={personal(false)}
        initialAvailability={noAvailability}
        actions={{ startConnect: vi.fn(), finishConnect: vi.fn(), disconnectCalendar: vi.fn(), saveMyAvailability: vi.fn() }}
      />,
      "ar",
    );
    expect(screen.getByRole("button", { name: "اربط Microsoft 365" })).toBeInTheDocument();
    expect(screen.getByText("ينشئ المسؤول مشروع Google Cloud لمؤسستك.")).toBeInTheDocument();
  });
});

describe("CalendarOrgSettings", () => {
  it("links the writes waiting for review and lists them with a link to the candidate", () => {
    renderWithIntl(
      <CalendarOrgSettings
        initial={org(false, 2)}
        operations={{
          total: 2,
          items: [
            { id: "op-1", provider: "microsoft", kind: "event", action: "create", rejected: false, attempts: 5, last_error: "microsoft_unavailable", created_at: "2026-10-14T08:00:00Z", updated_at: "2026-10-14T09:00:00Z", interview_id: "iv-1", job_id: JOB, application_id: APP },
            { id: "op-2", provider: "zoom", kind: "meeting", action: "cancel", rejected: true, attempts: 1, last_error: null, created_at: "2026-10-14T08:00:00Z", updated_at: "2026-10-14T08:30:00Z", interview_id: "iv-2", job_id: null, application_id: null },
          ],
        }}
        actions={{ saveZoom: vi.fn(), removeZoom: vi.fn() }}
      />,
    );
    const link = screen.getByRole("link", { name: "2 calendar changes are waiting for review" });
    expect(link).toHaveAttribute("href", "#calendar-review");
    const list = document.getElementById("calendar-review") as HTMLElement;
    expect(within(list).getByRole("link", { name: "Open the candidate" })).toHaveAttribute(
      "href",
      `/hire/jobs/${JOB}/pipeline?candidate=${APP}`,
    );
    expect(within(list).getAllByRole("listitem")).toHaveLength(2);
    expect(screen.getByText("2 people connected")).toBeInTheDocument();
    expect(screen.getByText("The organisation's Zoom app (the form below)")).toBeInTheDocument();
  });

  it("saves the Zoom app, clears the secret, and never shows a saved one", async () => {
    const saveZoom = vi.fn().mockResolvedValue({ ok: true, org: org(true, 0) });
    renderWithIntl(
      <CalendarOrgSettings initial={org(false, 0)} operations={null} actions={{ saveZoom, removeZoom: vi.fn() }} />,
    );
    expect(screen.getByText("No calendar changes are waiting for review.")).toBeInTheDocument();
    await userEvent.type(screen.getByLabelText("Account id"), "acct-1");
    await userEvent.type(screen.getByLabelText("Client id"), "zoom-client");
    const secret = screen.getByLabelText("Client secret");
    expect(secret).toHaveAttribute("type", "password");
    await userEvent.type(secret, "zoom-secret-1");
    await userEvent.click(screen.getByRole("button", { name: "Check and save" }));
    await waitFor(() =>
      expect(saveZoom).toHaveBeenCalledWith({ accountId: "acct-1", clientId: "zoom-client", clientSecret: "zoom-secret-1", host: "" }),
    );
    expect(await screen.findByText("Zoom app checked and saved.")).toBeInTheDocument();
    expect(screen.getByLabelText("Client secret")).toHaveValue("");
    expect(document.body.textContent).not.toContain("zoom-secret-1");
    expect(screen.getByRole("button", { name: "Remove Zoom app" })).toBeInTheDocument();
  });

  it("shows the Arabic plural for the review count", () => {
    renderWithIntl(
      <CalendarOrgSettings initial={org(true, 3)} operations={null} actions={{ saveZoom: vi.fn(), removeZoom: vi.fn() }} />,
      "ar",
    );
    expect(screen.getByRole("link", { name: "3 تغييرات في التقويم بانتظار المراجعة" })).toBeInTheDocument();
  });
});
