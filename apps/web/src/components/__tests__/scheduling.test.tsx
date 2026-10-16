// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { ScheduleView } from "@/lib/actions/candidate-schedule";
import {
  addDays,
  clock,
  groupByDay,
  instantAt,
  localDate,
  minutesOfDay,
  type Interview,
  type SchedulingPanelData,
  type SlotsResult,
} from "@/lib/hire/scheduling";

import { renderWithIntl } from "../../../test/intl";

vi.mock("@/lib/actions/scheduling", () => ({
  loadScheduling: vi.fn(),
  findSlots: vi.fn(),
  checkSlot: vi.fn(),
  createInterview: vi.fn(),
  updateInterview: vi.fn(),
  cancelInterview: vi.fn(),
  resendLink: vi.fn(),
  recordOutcome: vi.fn(),
  jobInterviews: vi.fn(),
  loadIntegrations: vi.fn(),
  startConnect: vi.fn(),
  finishConnect: vi.fn(),
  disconnectCalendar: vi.fn(),
  loadOrgIntegrations: vi.fn(),
  saveZoom: vi.fn(),
  removeZoom: vi.fn(),
  myAvailability: vi.fn(),
  saveMyAvailability: vi.fn(),
}));
vi.mock("@/lib/actions/candidate-schedule", () => ({
  readSchedule: vi.fn(),
  bookSchedule: vi.fn(),
  cancelSchedule: vi.fn(),
}));

const { SchedulingPanel } = await import("../hire/scheduling/scheduling-panel");
const { CandidateSchedule } = await import("../hire/scheduling/candidate-schedule");
const { CalendarSettings } = await import("../settings/calendar-settings");

const APP = "11111111-1111-4111-8111-111111111111";
const ME = "22222222-2222-4222-8222-222222222222";
const OMAR = "33333333-3333-4333-8333-333333333333";

describe("time helpers", () => {
  it("formats and inverts local times across zones and DST", () => {
    expect(clock("2026-10-13T06:00:00Z", "Asia/Dubai")).toBe("10:00");
    expect(clock("2026-10-13T06:00:00Z", "America/New_York")).toBe("02:00");
    expect(localDate("2026-10-12T22:30:00Z", "Asia/Dubai")).toBe("2026-10-13");
    expect(minutesOfDay("2026-03-30T08:00:00Z", "Europe/London")).toBe(9 * 60);
    // 09:00 in London is 09:00 UTC before the clocks go forward and 08:00 UTC after.
    expect(instantAt("2026-03-27", 9 * 60, "Europe/London").toISOString()).toBe("2026-03-27T09:00:00.000Z");
    expect(instantAt("2026-03-30", 9 * 60, "Europe/London").toISOString()).toBe("2026-03-30T08:00:00.000Z");
    expect(instantAt("2026-11-02", 9 * 60, "America/New_York").toISOString()).toBe("2026-11-02T14:00:00.000Z");
    expect(addDays("2026-12-31", 1)).toBe("2027-01-01");
    expect(addDays("2026-03-01", -1)).toBe("2026-02-28");
    const grouped = groupByDay(
      [{ start: "2026-10-12T20:30:00Z" }, { start: "2026-10-12T21:00:00Z" }],
      "Asia/Dubai",
    );
    expect(grouped.map((g) => g.date)).toEqual(["2026-10-13"]);
  });
});

function panelData(interviews: Interview[] = []): SchedulingPanelData {
  return {
    application_id: APP,
    job_id: "44444444-4444-4444-8444-444444444444",
    label: "Candidate ABCD-1234",
    identity_hidden: true,
    job_title: "Senior Backend Engineer",
    stage_key: "shortlist",
    stage_label: "Shortlist",
    stage_label_ar: "القائمة المختصرة",
    candidate_timezone: "Europe/London",
    viewer_timezone: "Asia/Dubai",
    duration_min: 45,
    step_min: 30,
    team: [
      { id: ME, name: "Rana", timezone: "Asia/Dubai" },
      { id: OMAR, name: "Omar", timezone: "Europe/London" },
    ],
    default_panel: [ME],
    interviews,
    google_enabled: false,
    google_configured: false,
  };
}

const SLOTS: SlotsResult = {
  timezone: "Europe/London",
  duration_min: 45,
  window_start: "2026-10-12T00:00:00Z",
  window_end: "2026-10-26T00:00:00Z",
  slots: [
    { start: "2026-10-13T06:00:00Z", end: "2026-10-13T06:45:00Z", sociable: false },
    { start: "2026-10-13T09:00:00Z", end: "2026-10-13T09:45:00Z", sociable: true },
  ],
  free: ["2026-10-13T06:00:00Z", "2026-10-13T09:00:00Z", "2026-10-13T09:30:00Z"],
  busy: [{ user_id: ME, blocks: [{ start: "2026-10-13T07:00:00Z", end: "2026-10-13T08:00:00Z", sociable: true }] }],
};

function interview(patch: Partial<Interview> = {}): Interview {
  return {
    id: "55555555-5555-4555-8555-555555555555",
    application_id: APP,
    job_id: "44444444-4444-4444-8444-444444444444",
    stage_key: "shortlist",
    status: "booked",
    starts_at: "2026-10-13T06:00:00Z",
    ends_at: "2026-10-13T06:45:00Z",
    duration_min: 45,
    candidate_timezone: "Europe/London",
    panel: [{ id: ME, name: "Rana", timezone: "Asia/Dubai" }],
    meeting_url: "https://meet.pairly.example/abc-defg-hij",
    meeting_provider: "fake",
    calendar_provider: "fake",
    ics_sequence: 0,
    link_expires_at: null,
    link_active: false,
    cancelled_by: null,
    can_record_outcome: false,
    created_at: "2026-10-09T10:00:00Z",
    ...patch,
  };
}

describe("SchedulingPanel", () => {
  it("books a suggested slot in one click, with both time zones shown", async () => {
    const created = interview();
    const actions = {
      loadScheduling: vi.fn().mockResolvedValue({ ok: true, data: panelData() }),
      findSlots: vi.fn().mockResolvedValue({ ok: true, slots: SLOTS }),
      createInterview: vi.fn().mockResolvedValue({ ok: true, interview: created }),
    };
    const onChanged = vi.fn();
    renderWithIntl(<SchedulingPanel applicationId={APP} onClose={() => {}} onChanged={onChanged} actions={actions} />);
    const slot = await screen.findByRole("button", { name: /Book Tue 13 Oct, 10:00, 07:00 for the candidate/ });
    expect(screen.getByText(/The panel sees Candidate ABCD-1234/)).toBeInTheDocument();
    expect(screen.getAllByText(/Early or late for the candidate/).length).toBe(1);
    await userEvent.click(slot);
    await waitFor(() => expect(actions.createInterview).toHaveBeenCalled());
    expect(actions.createInterview.mock.calls[0]?.[0]).toMatchObject({
      applicationId: APP,
      mode: "book",
      panel: [ME],
      durationMin: 45,
      startsAt: "2026-10-13T06:00:00Z",
      candidateTimezone: "Europe/London",
    });
    await waitFor(() => expect(onChanged).toHaveBeenCalledWith(created));
  });

  it("moves a booked interview, and shows conflicts inline for a busy time", async () => {
    const booked = interview();
    const actions = {
      loadScheduling: vi.fn().mockResolvedValue({ ok: true, data: panelData([booked]) }),
      findSlots: vi.fn().mockResolvedValue({ ok: true, slots: SLOTS }),
      updateInterview: vi.fn().mockResolvedValue({ ok: true, interview: { ...booked, status: "rescheduled" } }),
      checkSlot: vi.fn().mockResolvedValue({
        ok: true,
        check: { ok: false, conflicts: [{ user_id: ME, name: "Rana", reason: "busy" }] },
      }),
    };
    renderWithIntl(<SchedulingPanel applicationId={APP} onClose={() => {}} actions={actions} />);
    expect(await screen.findByText("Booked")).toBeInTheDocument();
    await userEvent.click(await screen.findByRole("button", { name: /Move to Tue 13 Oct, 13:00/ }));
    await waitFor(() =>
      expect(actions.updateInterview).toHaveBeenCalledWith(booked.id, {
        startsAt: "2026-10-13T09:00:00Z",
        panel: [ME],
        durationMin: 45,
        force: false,
      }),
    );
    // Keyboard on the calendar block: down one step, Enter, conflict shown inline.
    const block = await screen.findByRole("button", { name: /Interview, Tue 13 Oct, 10:00/ });
    block.focus();
    await userEvent.keyboard("{ArrowDown}{Enter}");
    expect(await screen.findByText(/Rana: busy/)).toBeInTheDocument();
    expect(actions.checkSlot.mock.calls[0]?.[0]).toMatchObject({ startsAt: "2026-10-13T06:30:00.000Z" });
    await userEvent.click(screen.getByRole("button", { name: "Move anyway" }));
    await waitFor(() =>
      expect(actions.updateInterview).toHaveBeenLastCalledWith(booked.id, expect.objectContaining({ force: true })),
    );
  });

  it("sends a self-scheduling link and renders in Arabic", async () => {
    const actions = {
      loadScheduling: vi.fn().mockResolvedValue({ ok: true, data: panelData() }),
      findSlots: vi.fn().mockResolvedValue({ ok: true, slots: SLOTS }),
      createInterview: vi.fn().mockResolvedValue({ ok: true, interview: interview({ status: "proposed", starts_at: null }) }),
    };
    renderWithIntl(<SchedulingPanel applicationId={APP} onClose={() => {}} actions={actions} />, "ar");
    await userEvent.click(await screen.findByRole("button", { name: /أرسل رابطا ليختار المرشح الموعد/ }));
    await waitFor(() => expect(actions.createInterview.mock.calls[0]?.[0]).toMatchObject({ mode: "link" }));
  });
});

describe("CandidateSchedule", () => {
  it("reads the token from the fragment, books a time and shows the confirmation", async () => {
    window.history.replaceState(null, "", "/en/schedule#hs1.token");
    const choose: ScheduleView = {
      kind: "ready",
      schedule: {
        state: "choose",
        organisation: "Acme Logistics",
        job_title: "Senior Backend Engineer",
        duration_min: 45,
        panel_size: 2,
        timezone: "UTC",
        starts_at: null,
        ends_at: null,
        meeting_url: null,
        slots: [
          { start: "2026-10-13T06:00:00Z", end: "2026-10-13T06:45:00Z", date: "2026-10-13", time: "06:00", sociable: false },
        ],
        token: null,
      },
    };
    const read = vi.fn().mockResolvedValue(choose);
    const book = vi.fn().mockResolvedValue({
      kind: "ready",
      schedule: {
        ...choose.schedule,
        state: "booked",
        starts_at: "2026-10-13T06:00:00Z",
        ends_at: "2026-10-13T06:45:00Z",
        meeting_url: "https://meet.pairly.example/abc",
        slots: [],
        token: "hs1.next",
      },
    } satisfies ScheduleView);
    renderWithIntl(<CandidateSchedule read={read} book={book} />);
    expect(await screen.findByText("Choose your interview time")).toBeInTheDocument();
    expect(read.mock.calls[0]?.[0]).toBe("hs1.token");
    expect(window.location.hash).toBe("");
    expect(screen.getByText(/2 people from the team/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /06:00/ }));
    await userEvent.click(screen.getByRole("button", { name: "Book this time" }));
    expect(await screen.findByText("Your interview is booked")).toBeInTheDocument();
    expect(book.mock.calls[0]?.[1]).toBe("2026-10-13T06:00:00Z");
    expect(screen.getByRole("link", { name: "Join the call" })).toHaveAttribute("href", "https://meet.pairly.example/abc");
  });

  it("explains a used link", async () => {
    window.history.replaceState(null, "", "/en/schedule#hs1.old");
    renderWithIntl(<CandidateSchedule read={vi.fn().mockResolvedValue({ kind: "used" })} />);
    expect(await screen.findByText("This link was used")).toBeInTheDocument();
  });
});

describe("CalendarSettings", () => {
  it("shows Google as not connected with the setup steps, and saves availability", async () => {
    const save = vi.fn().mockResolvedValue({ ok: true, availability: {} });
    renderWithIntl(
      <CalendarSettings
        initialIntegrations={{
          using: "fake",
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
          ],
        }}
        initialAvailability={{
          user_id: ME,
          name: "Rana",
          saved: false,
          timezone: "Asia/Dubai",
          working_hours: { mon: [["09:00", "17:00"]] },
          buffer_before_min: 0,
          buffer_after_min: 15,
          max_per_day: 4,
          holidays: [],
        }}
        actions={{ startConnect: vi.fn(), finishConnect: vi.fn(), disconnectCalendar: vi.fn(), saveMyAvailability: save }}
      />,
    );
    expect(screen.getByText("Not connected")).toBeInTheDocument();
    expect(screen.getByText(/creates a Google Cloud project/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Connect Google Calendar" })).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() =>
      expect(save).toHaveBeenCalledWith(
        expect.objectContaining({ timezone: "Asia/Dubai", workingHours: { mon: [["09:00", "17:00"]] }, maxPerDay: 4 }),
      ),
    );
  });
});
