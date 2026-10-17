// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { HireBiasGroup, HireBiasReport } from "@/lib/hire/types";

import { renderWithIntl } from "../../../test/intl";

vi.mock("@/lib/actions/fairness", () => ({ startFairnessRun: vi.fn() }));

const { BiasReport } = await import("../hire/bias-report");
const { FairnessRunButton } = await import("../hire/fairness-run-button");

/** Heading levels in document order; each one may go at most one level deeper than the last. */
function headingLevels(): number[] {
  return Array.from(document.querySelectorAll("h1, h2, h3, h4, h5, h6")).map((h) => Number(h.tagName[1]));
}
function expectNoSkippedLevels(levels: number[], start: number): void {
  expect(levels[0]).toBe(start);
  levels.slice(1).forEach((level, i) => expect(level - (levels[i] ?? start)).toBeLessThanOrEqual(1));
}

function group(patch: Partial<HireBiasGroup>): HireBiasGroup {
  return {
    group: "man",
    label_en: "Man",
    label_ar: "رجل",
    suppressed: false,
    base: 50,
    selected: 20,
    rate: 0.4,
    impact_ratio: 1,
    small_sample: false,
    fisher_p: null,
    judgement: "reference",
    ...patch,
  };
}

const REPORT: HireBiasReport = {
  generated_at: "2026-10-13T09:00:00+00:00",
  job_id: null,
  since: null,
  until: null,
  applications: 121,
  settings: { min_sample: 30, min_group: 5 },
  stages: ["applied", "screening", "hired"],
  categories: [
    {
      category: "gender",
      label_en: "Gender",
      label_ar: "الجنس",
      steps: [
        {
          from_stage: "applied",
          to_stage: "screening",
          reference_group: "man",
          groups: [
            group({}),
            group({ group: "woman", label_en: "Woman", label_ar: "امرأة", base: 40, selected: 10, rate: 0.25, impact_ratio: 0.625, judgement: "below_four_fifths" }),
            group({ group: "another", label_en: "Another gender", label_ar: "جنس آخر", suppressed: true, base: null, selected: null, rate: null, impact_ratio: null, judgement: "suppressed" }),
            group({ group: "unanswered", label_en: "Not answered", label_ar: "لم يجب", base: 9, selected: 2, rate: 0.2222, impact_ratio: null, small_sample: true, fisher_p: 0.31, judgement: "too_small_to_judge" }),
          ],
        },
      ],
    },
  ],
  fairness: null,
  notes: [],
};

describe("BiasReport", () => {
  it("shows rates, ratios and judgements, and hides small groups", () => {
    renderWithIntl(<BiasReport report={REPORT} exportHref="/api/hire/bias/export" stageLabels={{ applied: "Applied", screening: "Screening" }} />);
    expect(screen.getByText("Applied to Screening")).toBeInTheDocument();
    const woman = document.querySelector('[data-group="woman"]') as HTMLElement;
    expect(within(woman).getByText("10 of 40")).toBeInTheDocument();
    expect(within(woman).getByText("25%")).toBeInTheDocument();
    expect(within(woman).getByText("0.63")).toBeInTheDocument();
    expect(within(woman).getByText("Below four-fifths")).toBeInTheDocument();
    const another = document.querySelector('[data-group="another"]') as HTMLElement;
    expect(within(another).getByText("Hidden: fewer than 5 applications")).toBeInTheDocument();
    expect(within(another).queryByText(/of/)).toBeNull();
    const unanswered = document.querySelector('[data-group="unanswered"]') as HTMLElement;
    expect(within(unanswered).getByText("Too small to judge")).toBeInTheDocument();
    expect(within(unanswered).getByText("0.31")).toBeInTheDocument();
  });

  it("offers the export only when allowed, and never claims compliance", () => {
    const { unmount } = renderWithIntl(<BiasReport report={REPORT} exportHref="/api/hire/bias/export" />);
    expect(screen.getByRole("link", { name: "Download the bias audit pack" })).toHaveAttribute("href", "/api/hire/bias/export");
    expect(screen.getByText(/does not by itself show compliance with any law/)).toBeInTheDocument();
    unmount();
    renderWithIntl(<BiasReport report={REPORT} exportHref={null} />);
    expect(screen.queryByRole("link", { name: "Download the bias audit pack" })).toBeNull();
  });

  it("keeps the heading outline: section h2, category h3, step h4", () => {
    renderWithIntl(<BiasReport report={REPORT} exportHref={null} />);
    const levels = headingLevels();
    expectNoSkippedLevels(levels, 2);
    expect(levels).toEqual([2, 2, 3, 4]);
    expect(screen.getByRole("heading", { level: 2, name: "Selection rates by group" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 3, name: "Gender" })).toBeInTheDocument();
  });

  it("underlines the audit pack link inside the text, in Arabic too", () => {
    renderWithIntl(<BiasReport report={REPORT} exportHref="/api/hire/bias/export" />, "ar");
    const link = screen.getByRole("link", { name: "نزّل حزمة تدقيق التحيز" });
    expect(link).toHaveClass("link-inline");
    expect(screen.getByRole("heading", { level: 2, name: "نسب الاختيار حسب الفئة" })).toBeInTheDocument();
  });

  it("renders in Arabic", () => {
    renderWithIntl(<BiasReport report={REPORT} exportHref={null} />, "ar");
    expect(screen.getByText("الجنس")).toBeInTheDocument();
    expect(screen.getByText("أقل من الأربعة أخماس")).toBeInTheDocument();
  });

  it("reports the fairness run honestly: redaction measured, model deltas pending", () => {
    const fairness = { pairs: 30, flagged: 0, redaction_complete: 29, input_identical: 28, model_deltas: "pending_live_run", threshold: 5 };
    renderWithIntl(<BiasReport report={{ ...REPORT, fairness }} exportHref={null} />);
    expect(screen.getByText(/30 copies of screened applications/)).toBeInTheDocument();
    expect(screen.getByText(/hid every inserted value in 29 of them/)).toBeInTheDocument();
    expect(screen.getByText(/Score changes: pending a live model run/)).toBeInTheDocument();
  });

  it("shows measured score changes from a live run, and no run yet", () => {
    const fairness = { pairs: 30, flagged: 2, redaction_complete: 30, input_identical: 30, model_deltas: "measured", threshold: 5 };
    const { unmount } = renderWithIntl(<BiasReport report={{ ...REPORT, fairness }} exportHref={null} />);
    expect(screen.getByText("2 copies changed the overall score by more than 5 points.")).toBeInTheDocument();
    unmount();
    renderWithIntl(<BiasReport report={REPORT} exportHref={null} />);
    expect(screen.getByText("No fairness run yet.")).toBeInTheDocument();
  });
});

describe("FairnessRunButton", () => {
  it("starts a run once and says so", async () => {
    const user = userEvent.setup();
    const start = vi.fn().mockResolvedValue({ ok: true, id: "x" });
    renderWithIntl(<FairnessRunButton start={start} />);
    await user.click(screen.getByRole("button", { name: "Run the fairness test now" }));
    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("Started."));
    expect(screen.getByRole("button", { name: "Run the fairness test now" })).toBeDisabled();
    expect(start).toHaveBeenCalledTimes(1);
  });

  it("says when it could not start", async () => {
    const user = userEvent.setup();
    renderWithIntl(<FairnessRunButton start={vi.fn().mockResolvedValue({ ok: false, code: "conflict" })} />);
    await user.click(screen.getByRole("button", { name: "Run the fairness test now" }));
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("Could not start the run."));
  });
});
