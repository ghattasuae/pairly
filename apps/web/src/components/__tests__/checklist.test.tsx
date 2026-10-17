// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { HireChecklist, HireChecklistConfig } from "@/lib/hire/types";

import { renderWithIntl } from "../../../test/intl";
import { navigationMock } from "../../../test/navigation-mock";

vi.mock("@/i18n/navigation", () => navigationMock);
vi.mock("@/lib/actions/checklist", () => ({ requestChecklist: vi.fn(), reviewChecklistItem: vi.fn(), saveChecklistConfig: vi.fn() }));

const actions = await import("@/lib/actions/checklist");
const { ChecklistPanel } = await import("../hire/checklist-panel");

const APP = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const JOB = "cccccccc-cccc-4ccc-8ccc-cccccccccccc";
const CONFIG: HireChecklistConfig = {
  job_id: JOB,
  items: [
    { key: "id_document", label: "Passport", label_ar: "جواز السفر" },
    { key: "degree", label: "Degree certificate", label_ar: "" },
  ],
  retention_days: 90,
  saved: true,
};

function checklist(patch: Partial<HireChecklist> = {}): HireChecklist {
  return {
    application_id: APP,
    job_id: JOB,
    retention_days: 90,
    items: [
      {
        id: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
        key: "id_document",
        label: "Passport",
        label_ar: "جواز السفر",
        status: "received",
        has_file: true,
        content_type: "application/pdf",
        size_bytes: 1200,
        received_at: "2026-10-13T08:00:00Z",
        reviewed_at: null,
        retention_until: null,
        purged_at: null,
      },
    ],
    missing: [{ key: "degree", label: "Degree certificate", label_ar: "" }],
    can_write: true,
    ...patch,
  };
}

describe("ChecklistPanel", () => {
  it("shows status, opens files and requests what is missing", async () => {
    const user = userEvent.setup();
    vi.mocked(actions.requestChecklist).mockResolvedValue({ ok: true, checklist: checklist({ missing: [] }) });
    renderWithIntl(<ChecklistPanel initial={checklist()} config={CONFIG} />);
    expect(screen.getByText(/removed 90 days after hire/)).toBeInTheDocument();
    expect(screen.getByText("Received")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Open the file" })).toHaveAttribute("href", "/api/hire/checklist-items/bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb/file");
    expect(screen.getByText("Not requested yet: Degree certificate.")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Request through the portal" }));
    expect(actions.requestChecklist).toHaveBeenCalledWith(APP);
    await waitFor(() => expect(screen.queryByText(/Not requested yet/)).not.toBeInTheDocument());
  });

  it("verifies a received document", async () => {
    const user = userEvent.setup();
    vi.mocked(actions.reviewChecklistItem).mockResolvedValue({
      ok: true,
      checklist: checklist({ items: checklist().items.map((i) => ({ ...i, status: "verified" })) }),
    });
    renderWithIntl(<ChecklistPanel initial={checklist()} config={CONFIG} />);
    await user.click(screen.getByRole("button", { name: "Mark verified" }));
    expect(actions.reviewChecklistItem).toHaveBeenCalledWith("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb", "verified");
    await waitFor(() => expect(screen.getByText("Verified")).toBeInTheDocument());
  });

  it("edits the job's documents and retention", async () => {
    const user = userEvent.setup();
    vi.mocked(actions.saveChecklistConfig).mockResolvedValue({ ok: true, config: { ...CONFIG, retention_days: 30 } });
    renderWithIntl(<ChecklistPanel initial={checklist()} config={CONFIG} />);
    await user.click(screen.getByText("Documents for this job"));
    await user.type(screen.getByLabelText(/New document/), "Police clearance");
    await user.click(screen.getByRole("button", { name: "Add" }));
    const days = screen.getByLabelText("Days to keep files after hire");
    await user.clear(days);
    await user.type(days, "30");
    await user.click(screen.getByRole("button", { name: "Save the documents" }));
    expect(actions.saveChecklistConfig).toHaveBeenCalledWith({
      jobId: JOB,
      items: [...CONFIG.items, { key: "police_clearance", label: "Police clearance", label_ar: "" }],
      retentionDays: 30,
    });
  });

  it("is read only without write access and reads in Arabic", () => {
    renderWithIntl(<ChecklistPanel initial={checklist({ can_write: false })} config={CONFIG} />, "ar");
    expect(screen.getByText("جواز السفر")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "تأكيد التحقق" })).not.toBeInTheDocument();
  });
});
