// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { RetentionHold, RetentionScheduleItem, RetentionSettings } from "@/lib/api/types";

import { renderWithIntl } from "../../../test/intl";
import { navigationMock } from "../../../test/navigation-mock";

vi.mock("@/i18n/navigation", () => navigationMock);
// Server actions are replaced by props; keep their modules out of jsdom.
vi.mock("@/lib/actions/settings", () => ({
  updateRetentionRule: vi.fn(),
  setRetentionCap: vi.fn(),
  placeRetentionHold: vi.fn(),
  releaseRetentionHold: vi.fn(),
  setRetentionPaused: vi.fn(),
  createApiKey: vi.fn(),
  revokeApiKey: vi.fn(),
  setFlag: vi.fn(),
  resetFlag: vi.fn(),
}));

const { RetentionPanel } = await import("../settings/retention-panel");

const CANDIDATE = "66666666-6666-4666-8666-666666666666";

const SETTINGS: RetentionSettings = {
  daily_cap: 200,
  preview_days: 7,
  hold_subjects: ["hire.candidate", "pay.vendor"],
  rules: [
    {
      key: "hire.candidates.closed",
      description: "Rejected and withdrawn candidates",
      mode: "subjects",
      workflow: "hire",
      enabled: true,
      default_enabled: true,
      days: 0,
      default_days: 0,
      configurable_days: false,
      customised: false,
      period: { unit: "months", value: 12, default: 12, minimum: 1, maximum: 120 },
      paused: false,
      paused_at: null,
      first_notice_at: "2026-10-16T02:15:00Z",
    },
    {
      key: "inbound_events.seen",
      description: "Ids of inbound provider events",
      mode: "rows",
      workflow: "core",
      enabled: true,
      default_enabled: true,
      days: 90,
      default_days: 90,
      configurable_days: true,
      customised: false,
      period: null,
      paused: false,
      paused_at: null,
      first_notice_at: null,
    },
  ],
};

const ITEM: RetentionScheduleItem = {
  id: "77777777-7777-4777-8777-777777777777",
  rule: "hire.candidates.closed",
  subject_type: "hire.candidate",
  subject_id: CANDIDATE,
  label: "APP-7Q2K9",
  due_at: "2026-10-01T00:00:00Z",
  listed_at: "2026-10-16T02:15:00Z",
  delete_after: "2026-10-23T02:15:00Z",
  status: "scheduled",
  attempts: 0,
  error: null,
  decision_id: null,
  deleted_at: null,
};

const HOLD: RetentionHold = {
  id: "88888888-8888-4888-8888-888888888888",
  subject_type: "hire.candidate",
  subject_id: CANDIDATE,
  placed_by: "99999999-9999-4999-8999-999999999999",
  placed_at: "2026-10-16T09:00:00Z",
  released_at: null,
  reason: "Tribunal claim",
};

describe("RetentionPanel", () => {
  it("lists what is scheduled for deletion with its date and places a legal hold", async () => {
    const hold = vi.fn().mockResolvedValue({ ok: true, hold: HOLD });
    renderWithIntl(
      <RetentionPanel initial={SETTINGS} schedule={[ITEM]} holds={[]} canManage totpEnabled hold={hold} />,
    );
    expect(screen.getByText("Rejected and withdrawn candidates")).toBeInTheDocument();
    expect(screen.getByText("Kept for 12 months")).toBeInTheDocument();
    const row = screen.getByText("APP-7Q2K9").closest("li") as HTMLElement;
    expect(within(row).getByText("Scheduled for deletion on Oct 23, 2026")).toBeInTheDocument();
    await userEvent.click(within(row).getByRole("button", { name: "Place a legal hold" }));
    await userEvent.type(screen.getByLabelText(/must APP-7Q2K9 be kept/), "Tribunal claim");
    await userEvent.click(screen.getByRole("button", { name: "Place the hold" }));
    expect(hold).toHaveBeenCalledWith({ subjectType: "hire.candidate", subjectId: CANDIDATE, reason: "Tribunal claim" });
    expect(await screen.findByText("Kept by a legal hold. It is not deleted while the hold stands.")).toBeInTheDocument();
    expect(screen.getByText("Tribunal claim")).toBeInTheDocument();
  });

  it("switches a rule off and changes the period in months", async () => {
    const off = { ...SETTINGS, rules: SETTINGS.rules.map((r) => (r.key === "hire.candidates.closed" ? { ...r, enabled: false } : r)) };
    const update = vi
      .fn()
      .mockResolvedValueOnce({ ok: true, settings: SETTINGS })
      .mockResolvedValueOnce({ ok: true, settings: off });
    renderWithIntl(<RetentionPanel initial={SETTINGS} schedule={[ITEM]} holds={[]} canManage totpEnabled update={update} />);
    const months = screen.getByLabelText("Months");
    await userEvent.clear(months);
    await userEvent.type(months, "18");
    const [save] = screen.getAllByRole("button", { name: "Save the period" });
    await userEvent.click(save as HTMLElement);
    expect(update).toHaveBeenCalledWith({ rule: "hire.candidates.closed", period: 18 });
    await userEvent.click(screen.getByRole("switch", { name: "Turn Rejected and withdrawn candidates on or off" }));
    expect(update).toHaveBeenLastCalledWith({ rule: "hire.candidates.closed", enabled: false });
    expect(await screen.findByText("Nothing is scheduled for deletion.")).toBeInTheDocument();
  });

  it("pauses a rule in one click and resumes it with a new preview", async () => {
    const paused = {
      ...SETTINGS,
      rules: SETTINGS.rules.map((r) => (r.key === "hire.candidates.closed" ? { ...r, paused: true, paused_at: "2026-10-17T08:00:00Z" } : r)),
    };
    const relisted = { ...ITEM, listed_at: "2026-10-20T08:00:00Z", delete_after: "2026-10-27T08:00:00Z" };
    const pause = vi
      .fn()
      .mockResolvedValueOnce({ ok: true, settings: paused, schedule: [ITEM] })
      .mockResolvedValueOnce({ ok: true, settings: SETTINGS, schedule: [relisted] });
    renderWithIntl(<RetentionPanel initial={SETTINGS} schedule={[ITEM]} holds={[]} canManage totpEnabled pause={pause} />);
    await userEvent.click(screen.getByRole("button", { name: "Pause Rejected and withdrawn candidates" }));
    expect(pause).toHaveBeenCalledWith("hire.candidates.closed", true);
    expect(await screen.findByText(/Paused since Oct 17, 2026\. Nothing of this rule is deleted/)).toBeInTheDocument();
    const row = screen.getByText("APP-7Q2K9").closest("li") as HTMLElement;
    expect(within(row).getByText("Not deleted while its rule is paused.")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Resume Rejected and withdrawn candidates" }));
    expect(pause).toHaveBeenLastCalledWith("hire.candidates.closed", false);
    expect(await screen.findByText("Scheduled for deletion on Oct 27, 2026")).toBeInTheDocument();
  });

  it("is read only without the manage permission, in Arabic too", () => {
    renderWithIntl(<RetentionPanel initial={SETTINGS} schedule={[ITEM]} holds={[{ ...HOLD, reason: null }]} canManage={false} totpEnabled={false} />, "ar");
    expect(screen.getByText("المرشحون المرفوضون والمنسحبون")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "ضع تجميدًا قانونيًا" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "إنهاء التجميد" })).not.toBeInTheDocument();
    for (const toggle of screen.getAllByRole("switch")) expect(toggle).toBeDisabled();
    expect(screen.queryByRole("button", { name: /إيقاف .* مؤقتا/ })).not.toBeInTheDocument();
  });
});
