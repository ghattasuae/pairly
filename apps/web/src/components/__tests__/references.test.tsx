// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { HireRefereePage, HireReferences } from "@/lib/hire/types";

import { renderWithIntl } from "../../../test/intl";
import { navigationMock } from "../../../test/navigation-mock";

vi.mock("@/i18n/navigation", () => navigationMock);
vi.mock("@/lib/actions/references", () => ({ askReferee: vi.fn(), resendReference: vi.fn(), cancelReference: vi.fn() }));
vi.mock("@/lib/actions/referee", () => ({ openReference: vi.fn(), loadReference: vi.fn(), saveReference: vi.fn() }));

const staff = await import("@/lib/actions/references");
const referee = await import("@/lib/actions/referee");
const { ReferencesPanel } = await import("../hire/references-panel");
const { RefereeForm } = await import("../hire/referee-form");

const APP = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";

function references(patch: Partial<HireReferences> = {}): HireReferences {
  return {
    application_id: APP,
    candidate_label: "Candidate 7KQ2-M9XD",
    identity_hidden: true,
    items: [
      {
        id: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
        label: "Referee 1",
        referee_name: null,
        referee_email: null,
        relationship: "manager",
        locale: "en",
        status: "submitted",
        sent_at: "2026-10-12T08:00:00Z",
        opened_at: "2026-10-12T09:00:00Z",
        submitted_at: "2026-10-12T09:10:00Z",
        answers: { company: "Gulf Logistics", would_rehire: "no", reliability: 2 },
      },
    ],
    submitted: 1,
    summary: {
      summary: "Referee 1 would not work with them again.",
      strengths: ["Calm"],
      development: [],
      inconsistencies: [],
      source: "model",
      prompt_version: "hire_reference_summary/v1",
      from_recording: true,
    },
    flags: [{ kind: "employer_not_on_cv", referee: "Referee 1", detail: "Referee 1 worked with the candidate at Gulf Logistics, which is not on the CV." }],
    can_write: true,
    ...patch,
  };
}

function page(patch: Partial<HireRefereePage> = {}): HireRefereePage {
  return {
    company: "Pairly Demo",
    candidate_name: "Layla Nasser",
    job_title: "Backend Engineer",
    referee_name: "Omar Haddad",
    status: "requested",
    answers: {},
    questions: [
      { id: "known_as", kind: "choice", required: true, label: "How do you know the candidate?", options: ["manager", "peer"] },
      { id: "company", kind: "line", required: true, label: "Where did you work together?", options: [] },
      { id: "reliability", kind: "rating", required: true, label: "How reliable?", options: [] },
      { id: "anything_else", kind: "text", required: false, label: "Anything else?", options: [] },
    ],
    ...patch,
  };
}

describe("ReferencesPanel", () => {
  it("keeps referees anonymous while blind and shows the summary and CV flags", () => {
    renderWithIntl(<ReferencesPanel initial={references()} />);
    expect(screen.getByText("Referee 1 would not work with them again.")).toBeInTheDocument();
    expect(screen.getByText(/not on the CV/)).toBeInTheDocument();
    expect(screen.getByText(/anonymous for now/)).toBeInTheDocument();
    expect(screen.queryByText("Omar Haddad")).not.toBeInTheDocument();
    expect(screen.getByText("Gulf Logistics")).toBeInTheDocument();
  });

  it("asks a referee with the form", async () => {
    const user = userEvent.setup();
    vi.mocked(staff.askReferee).mockResolvedValue({ ok: true, references: references({ submitted: 1 }) });
    renderWithIntl(<ReferencesPanel initial={references({ items: [], submitted: 0, summary: null, flags: [] })} />);
    expect(screen.getByText("No referee asked yet.")).toBeInTheDocument();
    await user.type(screen.getByLabelText("Referee's name"), "Omar Haddad");
    await user.type(screen.getByLabelText("Referee's email"), "omar@example.test");
    await user.click(screen.getByRole("button", { name: "Send the request" }));
    expect(staff.askReferee).toHaveBeenCalledWith({
      applicationId: APP,
      name: "Omar Haddad",
      email: "omar@example.test",
      relationship: "manager",
      locale: "en",
    });
  });

  it("hides the request form from people who only read", () => {
    renderWithIntl(<ReferencesPanel initial={references({ can_write: false })} />);
    expect(screen.queryByRole("button", { name: "Send the request" })).not.toBeInTheDocument();
  });
});

describe("RefereeForm", () => {
  it("gives clear context, saves progress and submits once every required answer is in", async () => {
    const user = userEvent.setup();
    vi.mocked(referee.saveReference).mockImplementation(async (answers, submit) => ({
      ok: true,
      page: page({ answers, status: submit ? "submitted" : "in_progress" }),
    }));
    renderWithIntl(<RefereeForm initial={page()} />);
    expect(screen.getByText(/Layla Nasser applied for Backend Engineer at Pairly Demo/)).toBeInTheDocument();
    expect(screen.getByText("0 of 3 required answers")).toBeInTheDocument();
    const submit = screen.getByRole("button", { name: "Submit the reference" });
    expect(submit).toBeDisabled();
    await user.click(screen.getByRole("radio", { name: "I was their manager" }));
    await user.click(screen.getByRole("button", { name: "Save and finish later" }));
    expect(referee.saveReference).toHaveBeenLastCalledWith({ known_as: "manager" }, false, "en");
    await waitFor(() => expect(screen.getByText(/Saved\./)).toBeInTheDocument());
    await user.type(screen.getByLabelText(/Where did you work together/), "Gulf Logistics");
    await user.click(screen.getByRole("radio", { name: "4" }));
    expect(screen.getByText("3 of 3 required answers")).toBeInTheDocument();
    await user.click(submit);
    expect(referee.saveReference).toHaveBeenLastCalledWith({ known_as: "manager", company: "Gulf Logistics", reliability: 4 }, true, "en");
    await waitFor(() => expect(screen.getByText(/Your reference is submitted/)).toBeInTheDocument());
  });

  it("shows the server's refusal in plain words", async () => {
    const user = userEvent.setup();
    vi.mocked(referee.saveReference).mockResolvedValue({ ok: false, code: "invalid_input" });
    renderWithIntl(<RefereeForm initial={page()} />);
    await user.click(screen.getByRole("button", { name: "Save and finish later" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/every question marked/);
  });
});
