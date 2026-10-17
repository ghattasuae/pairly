// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { HireScorecardPage } from "@/lib/hire/types";

import { renderWithIntl } from "../../../test/intl";
import { navigationMock } from "../../../test/navigation-mock";

vi.mock("@/i18n/navigation", () => navigationMock);
vi.mock("@/lib/actions/scorecard", () => ({
  openScorecard: vi.fn(),
  snoozeFromLink: vi.fn(),
  loadScorecard: vi.fn(),
  saveScorecard: vi.fn(),
  snoozeScorecard: vi.fn(),
}));

const actions = await import("@/lib/actions/scorecard");
const { ScorecardForm, ScorecardApp } = await import("../hire/scorecard");

const ORG = "0123456789abcdef0123456789abcdef";
const LINK = `sk1.${ORG}.${"1".repeat(32)}.${"2".repeat(32)}.${"A".repeat(43)}`;

function page(patch: Partial<HireScorecardPage["scorecard"]> = {}): HireScorecardPage {
  return {
    kit: null,
    scorecard: {
      interview_id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
      application_id: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
      job_id: "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
      job_title: "Backend Engineer",
      candidate_label: "Candidate 7KQ2-M9XD",
      identity_hidden: true,
      stage_key: "interview",
      stage_name: "Interview",
      starts_at: "2026-10-13T06:00:00Z",
      ends_at: "2026-10-13T06:45:00Z",
      criteria: ["python", "sql", "design"].map((id) => ({
        id,
        name: id.toUpperCase(),
        question: `Tell me about ${id}.`,
        anchors: ["None", "Some", "Solid", "Strong"],
      })),
      ratings: {},
      notes: "",
      recommendation: null,
      status: "draft",
      submitted_at: null,
      due_at: "2026-10-14T06:45:00Z",
      sla_state: "on_time",
      snoozed_until: null,
      others: null,
      others_submitted: 1,
      panel_size: 2,
      ...patch,
    },
  };
}

afterEach(() => {
  vi.clearAllMocks();
  window.location.hash = "";
});

describe("ScorecardForm", () => {
  it("submits only once every criterion and the recommendation are chosen, then shows others", async () => {
    const user = userEvent.setup();
    const submitted = page({
      status: "submitted",
      submitted_at: "2026-10-13T07:00:00Z",
      ratings: { python: 3, sql: 3, design: 4 },
      recommendation: "yes",
      others: [
        {
          interviewer_id: "dddddddd-dddd-4ddd-8ddd-dddddddddddd",
          interviewer_name: "Omar",
          ratings: { python: 2, sql: 3, design: 3 },
          notes: "Good on SQL.",
          recommendation: "no",
          submitted_at: "2026-10-13T06:50:00Z",
        },
      ],
    });
    vi.mocked(actions.saveScorecard).mockResolvedValue({ ok: true, page: submitted });
    renderWithIntl(<ScorecardForm initial={page()} />);
    expect(screen.getByText(/1 other interviewer has submitted/i)).toBeInTheDocument();
    expect(screen.queryByText("Omar")).not.toBeInTheDocument();
    const submit = screen.getByRole("button", { name: /submit scorecard/i });
    expect(submit).toBeDisabled();
    for (const name of ["PYTHON", "SQL", "DESIGN"]) {
      const group = screen.getByRole("radiogroup", { name });
      await user.click(group.querySelector('[data-rating="3"]') as HTMLElement);
    }
    expect(submit).toBeDisabled();
    await user.click(screen.getByRole("radio", { name: /^yes$/i }));
    expect(submit).toBeEnabled();
    await user.click(submit);
    expect(actions.saveScorecard).toHaveBeenCalledWith(
      { ratings: { python: 3, sql: 3, design: 3 }, notes: "", recommendation: "yes" },
      true,
    );
    await waitFor(() => expect(screen.getByText("Omar · No")).toBeInTheDocument());
    expect(screen.getByText(/submitted\. thank you/i)).toBeInTheDocument();
  });
});

describe("ScorecardApp", () => {
  it("posts the link from the fragment once, then loads the scorecard", async () => {
    window.location.hash = `#${LINK}`;
    vi.mocked(actions.openScorecard).mockResolvedValue({ ok: true });
    vi.mocked(actions.loadScorecard).mockResolvedValue({ ok: true, page: page() });
    renderWithIntl(<ScorecardApp />);
    await waitFor(() => expect(screen.getByText("Candidate 7KQ2-M9XD")).toBeInTheDocument());
    expect(actions.openScorecard).toHaveBeenCalledTimes(1);
    expect(actions.openScorecard).toHaveBeenCalledWith(LINK);
    expect(window.location.hash).toBe("");
  });

  it("explains a used link", async () => {
    window.location.hash = `#${LINK}`;
    vi.mocked(actions.openScorecard).mockResolvedValue({ ok: false, code: "not_found" });
    vi.mocked(actions.loadScorecard).mockResolvedValue({ ok: false, code: "not_signed_in" });
    renderWithIntl(<ScorecardApp />);
    await waitFor(() => expect(screen.getByText(/has been used or has expired/i)).toBeInTheDocument());
  });
});
