// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { HireSurveyForm } from "@/lib/hire/types";

import { renderWithIntl } from "../../../test/intl";
import { navigationMock } from "../../../test/navigation-mock";

vi.mock("@/i18n/navigation", () => navigationMock);
vi.mock("@/lib/actions/survey", () => ({ viewSurvey: vi.fn(), answerSurvey: vi.fn() }));

const actions = await import("@/lib/actions/survey");
const { SurveyForm } = await import("../hire/survey-form");

const TOKEN = `sv1.${"a".repeat(32)}.${"b".repeat(32)}.${"C".repeat(43)}`;
const FORM: HireSurveyForm = {
  company: "Acme Logistics",
  job_title: "Backend Engineer",
  questions: [
    { id: "overall", kind: "rating", required: true, label: "Overall, how was your experience?", options: [] },
    { id: "communication", kind: "rating", required: false, label: "How clear were our updates?", options: [] },
    { id: "respect", kind: "rating", required: false, label: "Treated fairly?", options: [] },
    { id: "recommend", kind: "choice", required: false, label: "Would you recommend us?", options: ["yes", "no", "unsure"] },
    { id: "comment", kind: "text", required: false, label: "Anything we could do better?", options: [] },
  ],
};

describe("SurveyForm", () => {
  it("is anonymous by default and needs the overall rating", async () => {
    const user = userEvent.setup();
    vi.mocked(actions.answerSurvey).mockResolvedValue({ ok: true });
    renderWithIntl(<SurveyForm token={TOKEN} form={FORM} />);
    expect(screen.getByText(/anonymous unless you choose/)).toBeInTheDocument();
    const send = screen.getByRole("button", { name: "Send my answers" });
    expect(send).toBeDisabled();
    await user.click(screen.getAllByRole("radio", { name: "4" })[0]!);
    await user.click(screen.getByRole("radio", { name: "Yes" }));
    await user.click(send);
    expect(actions.answerSurvey).toHaveBeenCalledWith(
      TOKEN,
      { overall: 4, communication: null, respect: null, recommend: "yes", comment: "" },
      false,
    );
    await waitFor(() => expect(screen.getByText(/Thank you/)).toBeInTheDocument());
  });

  it("attaches the name only when the candidate chooses to", async () => {
    const user = userEvent.setup();
    vi.mocked(actions.answerSurvey).mockResolvedValue({ ok: true });
    renderWithIntl(<SurveyForm token={TOKEN} form={FORM} />);
    await user.click(screen.getAllByRole("radio", { name: "2" })[0]!);
    await user.click(screen.getByLabelText(/Add my name/));
    await user.click(screen.getByRole("button", { name: "Send my answers" }));
    expect(vi.mocked(actions.answerSurvey).mock.lastCall?.[2]).toBe(true);
  });

  it("reads in Arabic and explains a used link", async () => {
    const user = userEvent.setup();
    vi.mocked(actions.answerSurvey).mockResolvedValue({ ok: false, code: "not_found" });
    renderWithIntl(<SurveyForm token={TOKEN} form={FORM} />, "ar");
    await user.click(screen.getAllByRole("radio", { name: "5" })[0]!);
    await user.click(screen.getByRole("button", { name: "إرسال إجاباتي" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("استخدم هذا الرابط");
  });
});
