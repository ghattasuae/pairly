// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
import { screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { HireSurveyResults } from "@/lib/hire/types";

import { renderWithIntl } from "../../../test/intl";

const { SurveyResults } = await import("../hire/survey-results");

const RESULTS: HireSurveyResults = {
  invited: 20,
  responses: 8,
  response_rate: 0.4,
  overall: {
    responses: 8,
    suppressed: false,
    averages: { overall: 4.25, communication: 3.5, respect: null },
    recommend: { yes: 5, no: 1, unsure: 2 },
  },
  by_outcome: {
    hired: { responses: 2, suppressed: true, averages: {}, recommend: {} },
    rejected: { responses: 6, suppressed: false, averages: { overall: 4, communication: 3, respect: 4.5 }, recommend: { yes: 3, no: 1, unsure: 2 } },
    withdrawn: { responses: 0, suppressed: true, averages: {}, recommend: {} },
  },
  min_group: 5,
};

describe("SurveyResults", () => {
  it("shows totals and averages, and hides small groups", () => {
    renderWithIntl(<SurveyResults results={RESULTS} named={null} />);
    expect(screen.getByText("40%")).toBeInTheDocument();
    const groups = screen.getAllByRole("region");
    const overall = groups.find((g) => within(g).queryByText("All outcomes"))!;
    expect(within(overall).getByText("4.25 out of 5")).toBeInTheDocument();
    expect(within(overall).getByText("No answers")).toBeInTheDocument();
    expect(within(overall).getByText("Yes 5, no 1, not sure 2")).toBeInTheDocument();
    const hired = groups.find((g) => within(g).queryByText("Hired"))!;
    expect(within(hired).getByText("Hidden: 2 answers, fewer than 5")).toBeInTheDocument();
    expect(screen.queryByText("Answers with a name attached")).toBeNull();
  });

  it("lists only the answers whose authors attached their name", () => {
    renderWithIntl(
      <SurveyResults
        results={RESULTS}
        named={[
          {
            response_id: "r1",
            application_id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            outcome: "rejected",
            answers: { overall: 4, communication: 3 },
            comment: "Clear process.",
            answered_on: "2026-10-01",
          },
        ]}
      />,
    );
    expect(screen.getByText("Answers with a name attached")).toBeInTheDocument();
    expect(screen.getByText("Clear process.")).toBeInTheDocument();
    expect(screen.getByText(/Overall experience: 4/)).toBeInTheDocument();
  });

  it("renders in Arabic", () => {
    renderWithIntl(<SurveyResults results={RESULTS} named={[]} />, "ar");
    expect(screen.getByText("كل النتائج")).toBeInTheDocument();
    expect(screen.getByText("لم يرفق أي مرشح اسمه.")).toBeInTheDocument();
  });
});
