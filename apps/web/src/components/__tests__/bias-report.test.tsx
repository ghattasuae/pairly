// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
import { screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { HireBiasGroup, HireBiasReport } from "@/lib/hire/types";

import { renderWithIntl } from "../../../test/intl";

const { BiasReport } = await import("../hire/bias-report");

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

  it("renders in Arabic", () => {
    renderWithIntl(<BiasReport report={REPORT} exportHref={null} />, "ar");
    expect(screen.getByText("الجنس")).toBeInTheDocument();
    expect(screen.getByText("أقل من الأربعة أخماس")).toBeInTheDocument();
  });
});
