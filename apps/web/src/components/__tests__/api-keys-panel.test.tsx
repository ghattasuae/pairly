// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { renderWithIntl } from "../../../test/intl";
import { navigationMock } from "../../../test/navigation-mock";

vi.mock("@/i18n/navigation", () => navigationMock);
// Server actions are replaced by props; keep their modules out of jsdom.
vi.mock("@/lib/actions/settings", () => ({
  createApiKey: vi.fn(),
  revokeApiKey: vi.fn(),
  setFlag: vi.fn(),
  resetFlag: vi.fn(),
}));

const { ApiKeysPanel } = await import("../settings/admin-panels");

const CREATED = {
  id: "33333333-3333-4333-8333-333333333333",
  name: "CI",
  prefix: "pk_test_abcd",
  mode: "test",
  scopes: ["core.document.read"],
  created_by: "44444444-4444-4444-8444-444444444444",
  created_at: "2026-10-16T08:00:00Z",
  expires_at: "2027-01-14T08:00:00Z",
  last_used_at: null,
  revoked_at: null,
  rate_limit_per_minute: null,
  key: "pk_test_abcd_secret",
};

function mode(): HTMLSelectElement {
  return screen.getByLabelText("Mode") as HTMLSelectElement;
}

describe("ApiKeysPanel key mode", () => {
  it("defaults to test in a sandbox, disables live and says why", async () => {
    const create = vi.fn().mockResolvedValue({ ok: true, key: CREATED });
    renderWithIntl(
      <ApiKeysPanel initial={[]} scopes={["core.document.read"]} totpEnabled sandbox create={create} />,
    );
    expect(mode().value).toBe("test");
    expect(screen.getByRole("option", { name: "Live" })).toBeDisabled();
    expect(screen.getByRole("option", { name: "Test" })).not.toBeDisabled();
    expect(screen.getByText(/This is a sandbox organisation/)).toBeInTheDocument();

    const user = userEvent.setup();
    await user.type(screen.getByLabelText("Name"), "CI");
    await user.click(screen.getByRole("checkbox"));
    await user.click(screen.getByRole("button", { name: "Create key" }));
    expect(create).toHaveBeenCalledWith(expect.objectContaining({ mode: "test", name: "CI" }));
  });

  it("defaults to live outside a sandbox and disables test", () => {
    renderWithIntl(<ApiKeysPanel initial={[]} scopes={[]} totpEnabled={false} />);
    expect(mode().value).toBe("live");
    expect(screen.getByRole("option", { name: "Test" })).toBeDisabled();
    expect(screen.getByText(/This organisation uses live keys/)).toBeInTheDocument();
  });

  it("explains the sandbox in Arabic too", () => {
    renderWithIntl(<ApiKeysPanel initial={[]} scopes={[]} totpEnabled sandbox />, "ar");
    expect(screen.getByText(/هذه مؤسسة تجريبية/)).toBeInTheDocument();
  });
});
