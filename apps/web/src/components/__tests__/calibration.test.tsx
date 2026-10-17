// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { HireCalibrationApplication, HireCalibrationReport, HireCalibrationSample } from "@/lib/hire/types";

import { renderWithIntl } from "../../../test/intl";

vi.mock("@/lib/actions/calibration", () => ({
  loadCalibrationItem: vi.fn(),
  saveCalibrationLabels: vi.fn(),
  suggestClarification: vi.fn(),
}));

const { CalibrationLabeller, CalibrationReportView } = await import("../hire/calibration-workspace");

const APP = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const JOB = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";
const ANCHORS = ["None", "Mentioned", "Some", "Good", "Strong"];
const CRITERIA = [
  { id: "go", name: "Go services", description: "Builds services in Go.", evidence: "Named Go projects.", anchors: ANCHORS },
  { id: "testing", name: "Testing", description: "Writes tests.", evidence: "Test coverage work.", anchors: ANCHORS },
];
const SAMPLE: HireCalibrationSample = {
  job_id: JOB,
  rubric_version: 1,
  criteria: CRITERIA,
  items: [{ application_id: APP, reference: "AB12-CD34", labelled: false }],
};
const ITEM: HireCalibrationApplication = {
  application_id: APP,
  reference: "AB12-CD34",
  blind: true,
  text: "[name]\nBackend engineer. Built Go services.",
  criteria: CRITERIA,
  my_scores: {},
};

describe("CalibrationLabeller", () => {
  it("shows the CV and the anchors, never the AI's scores, and saves every criterion", async () => {
    const user = userEvent.setup();
    const load = vi.fn().mockResolvedValue({ ok: true, item: ITEM });
    const save = vi.fn().mockResolvedValue({ ok: true, item: { ...ITEM, my_scores: { go: 3, testing: 1 } } });
    renderWithIntl(<CalibrationLabeller sample={SAMPLE} load={load} save={save} />);
    await user.click(screen.getByRole("button", { name: /AB12-CD34/ }));
    expect(await screen.findByText(/Built Go services/)).toBeInTheDocument();
    expect(screen.getByText(/You will not see the AI's scores here/)).toBeInTheDocument();
    const saveButton = screen.getByRole("button", { name: "Save my scores" });
    expect(saveButton).toBeDisabled();
    await user.click(screen.getByRole("radio", { name: "Go services: 3" }));
    expect(saveButton).toBeDisabled(); // every criterion needs a score
    await user.click(screen.getByRole("radio", { name: "Testing: 1" }));
    await user.click(saveButton);
    await waitFor(() => expect(save).toHaveBeenCalledWith(APP, { go: 3, testing: 1 }));
    expect(await screen.findByRole("status")).toHaveTextContent("Saved.");
  });

  it("says when there is nothing to label", () => {
    renderWithIntl(<CalibrationLabeller sample={{ ...SAMPLE, items: [] }} />);
    expect(screen.getByText("No scored applications for this job yet.")).toBeInTheDocument();
  });
});

const REPORT: HireCalibrationReport = {
  job_id: JOB,
  rubric_version: 1,
  labels: 12,
  overrides: 1,
  criteria: [
    {
      criterion_id: "go",
      name: "Go services",
      n: 7,
      exact: 0,
      within_one: 0.2857,
      kappa: null,
      kappa_note: "needs at least 10 pairs",
      mean_difference: 2,
      sources: { label: 6, override: 1 },
      top_disagreement: { ai: 1, people: 3, count: 4 },
      low_agreement: true,
      direction: "people_higher",
      suggestion: "Most disagreements on Go services are the AI giving 1 where people give 3.",
    },
    {
      criterion_id: "testing",
      name: "Testing",
      n: 12,
      exact: 0.9167,
      within_one: 1,
      kappa: 0.88,
      kappa_note: null,
      mean_difference: 0,
      sources: { label: 12 },
      top_disagreement: null,
      low_agreement: false,
      direction: null,
      suggestion: null,
    },
  ],
};

describe("CalibrationReportView", () => {
  it("shows agreement, kappa only where n allows, and the suggestion from the anchors", () => {
    renderWithIntl(<CalibrationReportView report={REPORT} jobId={JOB} />);
    expect(screen.getByText("From 12 labels and 1 recorded overrides.")).toBeInTheDocument();
    expect(screen.getByText(/7 pairs: exact 0%, within one point 29%/)).toBeInTheDocument();
    expect(screen.getByText(/Kappa: needs at least 10 pairs/)).toBeInTheDocument();
    expect(screen.getByText(/Cohen's kappa 0.88/)).toBeInTheDocument();
    expect(screen.getByText("Low agreement")).toBeInTheDocument();
    expect(screen.getByText(/the AI giving 1 where people give 3/)).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: "Ask the AI for a suggestion" })).toHaveLength(1);
  });

  it("shows a model suggestion, or says plainly that none is available", async () => {
    const user = userEvent.setup();
    const suggest = vi
      .fn()
      .mockResolvedValueOnce({ ok: true, suggestion: { criterion_id: "go", suggestion: "Name the Go projects that count.", from_recording: true } })
      .mockResolvedValueOnce({ ok: false, code: "conflict" });
    renderWithIntl(<CalibrationReportView report={REPORT} jobId={JOB} suggest={suggest} />);
    const ask = screen.getByRole("button", { name: "Ask the AI for a suggestion" });
    await user.click(ask);
    expect(await screen.findByText(/Name the Go projects that count/)).toBeInTheDocument();
    expect(suggest).toHaveBeenCalledWith(JOB, "go");
    await user.click(ask);
    expect(await screen.findByRole("alert")).toHaveTextContent("No AI suggestion is available");
  });

  it("renders in Arabic", () => {
    renderWithIntl(<CalibrationReportView report={REPORT} jobId={JOB} />, "ar");
    expect(screen.getByText("توافق منخفض")).toBeInTheDocument();
  });
});
