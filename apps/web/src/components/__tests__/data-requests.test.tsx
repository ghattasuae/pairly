// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { HireDataRequest } from "@/lib/hire/types";

import { renderWithIntl } from "../../../test/intl";

vi.mock("@/lib/actions/data-requests", () => ({
  loadDataRequest: vi.fn(),
  resolveDataRequest: vi.fn(),
  eraseDataRequest: vi.fn(),
}));

const { DataRequests } = await import("../hire/data-requests");

const OPEN: HireDataRequest = {
  id: "11111111-1111-4111-8111-111111111111",
  candidate_id: "22222222-2222-4222-8222-222222222222",
  candidate_name: "Rami Haddad",
  candidate_email: "rami@example.test",
  kind: "deletion",
  status: "open",
  note: "Please delete my CV.",
  outcome: null,
  created_at: "2026-09-01T08:00:00Z",
  due_at: "2026-10-01T08:00:00Z",
  handled_at: null,
  erasure_pending: false,
};

describe("DataRequests", () => {
  it("lists requests with their deadline and erases only after a reason and a confirmation", async () => {
    const user = userEvent.setup();
    const load = vi.fn().mockResolvedValue({ ok: true, request: OPEN });
    const resolve = vi.fn();
    const erased: HireDataRequest = {
      ...OPEN,
      status: "completed",
      candidate_name: "Erased candidate",
      candidate_email: "erased@erased.invalid",
      outcome: "Asked by email to delete everything.",
      handled_at: "2026-10-10T08:00:00Z",
    };
    const erase = vi.fn().mockResolvedValue({ ok: true, request: erased });
    renderWithIntl(
      <DataRequests
        requests={[OPEN]}
        load={load}
        resolve={resolve}
        erase={erase}
        now={new Date("2026-10-10T00:00:00Z")}
      />,
    );

    expect(screen.getByText("Deletion request")).toBeInTheDocument();
    expect(screen.getByText("Overdue")).toBeInTheDocument();
    expect(screen.getByText(/^Due /)).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /Deletion request/ }));
    expect(load).toHaveBeenCalledWith(OPEN.id);
    expect(await screen.findByText("Please delete my CV.")).toBeInTheDocument();

    const eraseButton = screen.getByRole("button", { name: "Erase personal data" });
    expect(eraseButton).toBeDisabled();
    await user.type(screen.getByLabelText("Reason (required)"), "Asked by email to delete everything.");
    expect(eraseButton).toBeEnabled();
    await user.click(eraseButton);
    expect(erase).not.toHaveBeenCalled();

    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Erase personal data" }));
    await waitFor(() =>
      expect(erase).toHaveBeenCalledWith({ id: OPEN.id, reason: "Asked by email to delete everything." }),
    );
    expect(await screen.findByText("Personal data erased. The request is completed.")).toBeInTheDocument();
    expect(resolve).not.toHaveBeenCalled();
  });

  it("keeps an erasure whose files remain open, shows the error and retries with the same reason", async () => {
    const user = userEvent.setup();
    const pending: HireDataRequest = {
      ...OPEN,
      candidate_name: "Erased candidate",
      candidate_email: "erased@erased.invalid",
      note: null,
      outcome: "Asked by email to delete everything.",
      erasure_pending: true,
      erasure_error: "object_store_delete_failed",
    };
    const load = vi.fn().mockResolvedValue({ ok: true, request: pending });
    const completed: HireDataRequest = {
      ...pending,
      status: "completed",
      erasure_pending: false,
      erasure_error: null,
      handled_at: "2026-10-10T08:05:00Z",
    };
    const erase = vi.fn().mockResolvedValue({ ok: true, request: completed });
    renderWithIntl(
      <DataRequests
        requests={[pending]}
        initialId={pending.id}
        load={load}
        resolve={vi.fn()}
        erase={erase}
        now={new Date("2026-09-10T00:00:00Z")}
      />,
    );

    const alert = screen.getByRole("alert");
    expect(within(alert).getByText("Erasure in progress")).toBeInTheDocument();
    expect(within(alert).getByText("Last attempt: object_store_delete_failed")).toBeInTheDocument();
    // Only the erasure can complete it: no "mark completed" while files remain.
    expect(screen.queryByRole("button", { name: "Mark completed" })).not.toBeInTheDocument();

    await user.click(within(alert).getByRole("button", { name: "Retry now" }));
    await waitFor(() =>
      expect(erase).toHaveBeenCalledWith({ id: pending.id, reason: "Asked by email to delete everything." }),
    );
    expect(await screen.findByText("Personal data erased. The request is completed.")).toBeInTheDocument();
  });
});
