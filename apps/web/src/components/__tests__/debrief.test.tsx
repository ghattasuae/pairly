// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { HireDebrief } from "@/lib/hire/types";

import { renderWithIntl } from "../../../test/intl";
import { navigationMock } from "../../../test/navigation-mock";

vi.mock("@/i18n/navigation", () => navigationMock);
vi.mock("@/lib/actions/interviews", () => ({ decideDebrief: vi.fn(), saveInterviewPlan: vi.fn() }));

const actions = await import("@/lib/actions/interviews");
const { DebriefView } = await import("../hire/debrief");

function debrief(patch: Partial<HireDebrief> = {}): HireDebrief {
  return {
    application_id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
    job_id: "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
    job_title: "Backend Engineer",
    candidate_label: "Candidate 7KQ2-M9XD",
    identity_hidden: true,
    stage_key: "interview",
    stage_name: "Interview",
    status: "ready",
    expected: 2,
    submitted: 2,
    waiting_for: [],
    criteria: [
      {
        id: "python",
        name: "Python",
        ratings: [
          { interviewer: "Omar", label: "Interviewer 1", rating: 1 },
          { interviewer: "Sara", label: "Interviewer 2", rating: 4 },
        ],
        mean: 2.5,
        spread: 3,
        disagreement: true,
        summary: "Split on Python.",
      },
    ],
    voices: [
      { label: "Interviewer 1", interviewer: "Omar", recommendation: "no", notes: "Thin." },
      { label: "Interviewer 2", interviewer: "Sara", recommendation: "yes", notes: "Strong." },
    ],
    summary: "Two interviewers disagree on Python.",
    disagreements: ["Python: ratings range from 1 to 4."],
    open_questions: ["What evidence is missing on Python?"],
    source: "model",
    prompt_version: "hire_debrief_summary/v1",
    from_recording: true,
    decision: null,
    can_decide: true,
    ...patch,
  };
}

describe("DebriefView", () => {
  it("waits for every scorecard and shows nothing else", () => {
    renderWithIntl(
      <DebriefView
        initial={debrief({ status: "waiting", submitted: 1, waiting_for: ["Sara"], criteria: [], voices: [], summary: null })}
        totpEnabled
      />,
    );
    expect(screen.getByText("1 of 2 scorecards are in.")).toBeInTheDocument();
    expect(screen.getByText("Waiting for: Sara.")).toBeInTheDocument();
    expect(screen.queryByText("Thin.")).not.toBeInTheDocument();
  });

  it("shows the summary, flags disagreement and records a decision with a reason", async () => {
    const user = userEvent.setup();
    vi.mocked(actions.decideDebrief).mockResolvedValue({
      ok: true,
      debrief: debrief({
        can_decide: false,
        decision: {
          decision: "hold",
          reason: "One more interview.",
          decided_by: "dddddddd-dddd-4ddd-8ddd-dddddddddddd",
          decided_at: "2026-10-13T08:00:00Z",
          decision_record_id: "dr_1",
        },
      }),
    });
    renderWithIntl(<DebriefView initial={debrief()} totpEnabled />);
    expect(screen.getByText("Two interviewers disagree on Python.")).toBeInTheDocument();
    expect(screen.getByText("Disagreement")).toBeInTheDocument();
    const record = screen.getByRole("button", { name: /record the decision/i });
    expect(record).toBeDisabled();
    await user.click(screen.getByRole("radio", { name: "Hold" }));
    await user.type(screen.getByLabelText("Reason"), "One more interview.");
    await user.click(record);
    expect(actions.decideDebrief).toHaveBeenCalledWith({
      applicationId: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
      stage: "interview",
      decision: "hold",
      reason: "One more interview.",
    });
    await waitFor(() => expect(screen.getByText("One more interview.")).toBeInTheDocument());
  });
});
