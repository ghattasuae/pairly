// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
import { screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { renderWithIntl } from "../../../test/intl";
import { navigationMock } from "../../../test/navigation-mock";

vi.mock("@/i18n/navigation", () => navigationMock);
vi.mock("@/lib/actions/hire", () => ({ referralLink: vi.fn(), rediscover: vi.fn() }));

const { HireReportLinks } = await import("../hire/hire-home");

const LABELS = { analytics: "Analytics", bias: "Bias", surveys: "Surveys", calibration: "Calibration" };

describe("HireReportLinks", () => {
  it("links only the reports the person may open", () => {
    renderWithIntl(<HireReportLinks permissions={["hire.analytics.read", "hire.survey.read"]} labels={LABELS} />);
    expect(screen.getByRole("link", { name: "Analytics" })).toHaveAttribute("href", "/hire/analytics");
    expect(screen.getByRole("link", { name: "Surveys" })).toHaveAttribute("href", "/hire/surveys");
    expect(screen.queryByRole("link", { name: "Bias" })).toBeNull();
    expect(screen.queryByRole("link", { name: "Calibration" })).toBeNull();
  });

  it("shows nothing without any report permission", () => {
    const { container } = renderWithIntl(<HireReportLinks permissions={["hire.job.read"]} labels={LABELS} />);
    expect(container.querySelector("[data-report-links]")).toBeNull();
  });
});
