// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
import { screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { HireAnalytics, HireMetric } from "@/lib/hire/types";

import { renderWithIntl } from "../../../test/intl";
import { navigationMock } from "../../../test/navigation-mock";

vi.mock("@/i18n/navigation", () => navigationMock);

const { HiringAnalytics } = await import("../hire/hiring-analytics");

const APP = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const JOB = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";

function metric(key: string, value: number | null, unit: string, patch: Partial<HireMetric> = {}): HireMetric {
  return {
    key,
    value,
    unit,
    n: value === null ? 0 : 2,
    records: value === null ? [] : [{ kind: "application", id: APP, job_id: JOB, label: "AB12-CD34" }],
    records_total: value === null ? 0 : 1,
    detail: {},
    ...patch,
  };
}

const DATA: HireAnalytics = {
  generated_at: "2026-10-13T09:00:00Z",
  job_id: null,
  since: null,
  until: null,
  applications: 2,
  settings: { sla_days: 7, ghost_days: 7, agreement_threshold: 60, minutes_per_booking: 30 },
  time_to_first_response: metric("time_to_first_response", 4, "hours", { detail: { pending: 1, mean: 4 } }),
  time_in_stage: [metric("time_in_stage.applied", 1.5, "days", { detail: { stage: "applied" } })],
  time_to_hire: metric("time_to_hire", null, "days"),
  stage_conversion: [
    metric("stage_conversion.applied", 0.5, "rate", {
      detail: { stage: "applied", next_stage: "screening", reached: 2, moved_on: 1 },
    }),
  ],
  sources: [metric("source.careers", 0, "rate", { detail: { source: "careers", applications: 2, shortlisted: 1, hired: 0 } })],
  sla: [metric("sla.anti_ghosting", 1, "count"), metric("sla.approvals", 0, "count", { records: [], records_total: 0 })],
  ghosting_rate: metric("ghosting_rate", 0.5, "rate"),
  interviewer_load: [],
  scheduling_time_saved: metric("scheduling_time_saved", 60, "minutes", { detail: { estimate: true } }),
  agreement: [metric("ai_human_agreement", 0.6667, "rate"), metric("ai_human_disagreement", 1, "count")],
  override_rate: metric("override_rate", 0.25, "rate"),
  flags: [metric("flags.injection", 1, "count"), metric("flags.claims", 0, "count"), metric("flags.ai_signals", 0, "count")],
  cost_per_screened_application: metric("cost_per_screened_application", 0, "usd", {
    detail: { recorded_mode: true },
  }),
  survey: {
    invited: 3,
    responses: 2,
    response_rate: 0.667,
    overall: { responses: 2, suppressed: true, averages: {}, recommend: {} },
    by_outcome: {},
    min_group: 5,
  },
  jobs: {},
};

describe("HiringAnalytics", () => {
  it("shows each number with the records behind it, and no data as no data", () => {
    renderWithIntl(<HiringAnalytics data={DATA} />);
    expect(screen.getByText("Based on 2 applications.")).toBeInTheDocument();
    expect(screen.getByText("4 h")).toBeInTheDocument();
    expect(screen.getByText("1 candidate has not heard from us yet.")).toBeInTheDocument();
    // Time to hire has no data: never shown as 0.
    const hire = document.querySelector('[data-metric="time_to_hire"]')!;
    expect(hire.textContent).toContain("No data yet");
    // Every number links to its records.
    const links = screen.getAllByRole("link", { name: "AB12-CD34" });
    expect(links[0]).toHaveAttribute("href", `/hire/jobs/${JOB}/pipeline?candidate=${APP}`);
    expect(document.querySelector('[data-records="sla.approvals"]')).toBeNull();
  });

  it("labels estimates, recorded mode and hidden survey groups", () => {
    renderWithIntl(<HiringAnalytics data={DATA} />);
    expect(screen.getByText(/Estimate: interviews the candidate booked/)).toBeInTheDocument();
    expect(screen.getByText(/Recorded mode: the model answers came from recordings/)).toBeInTheDocument();
    expect(screen.getByText("Hidden: fewer than 5 answers")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Open the survey results" })).toHaveAttribute("href", "/hire/surveys");
  });

  it("renders in Arabic", () => {
    renderWithIntl(<HiringAnalytics data={DATA} />, "ar");
    expect(screen.getByText("بناءً على 2 طلبات.")).toBeInTheDocument();
  });
});
