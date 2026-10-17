// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { loadSlackToken, PENDING_SLACK_KEY, readSlackToken } from "@/lib/slack-connect-token";

import { renderWithIntl } from "../../../test/intl";
import { navigationMock, routerSpies } from "../../../test/navigation-mock";

vi.mock("@/i18n/navigation", () => navigationMock);
vi.mock("next/navigation", () => ({ useRouter: () => routerSpies }));
vi.mock("@/lib/auth/client", () => ({ authClient: { signOut: vi.fn(), signIn: { magicLink: vi.fn() } } }));
vi.mock("@/lib/actions/slack", () => ({ confirmSlackConnect: vi.fn() }));

const { SlackConnectLanding } = await import("../integrations/slack-connect-landing");

const TOKEN = "A".repeat(43);
const SSO = { google: false, microsoft: false };

function openWith(hash: string) {
  window.history.replaceState(null, "", `/en/integrations/slack/connect${hash}`);
}

afterEach(() => {
  localStorage.clear();
});

describe("Slack connect page", () => {
  it("reads the token from the fragment only", () => {
    expect(readSlackToken(`#${TOKEN}`)).toBe(TOKEN);
    expect(readSlackToken(`#token=${TOKEN}`)).toBe(TOKEN);
    expect(readSlackToken("#short")).toBeNull();
  });

  it("asks a signed-out person to sign in and keeps the link for when they come back", async () => {
    openWith(`#${TOKEN}`);
    renderWithIntl(<SlackConnectLanding sso={SSO} email={null} />);
    expect(await screen.findByText("Sign in to connect Slack")).toBeInTheDocument();
    expect(window.location.hash).toBe("");
    expect(loadSlackToken()).toBe(TOKEN);
  });

  it("links the accounts only after the explicit confirmation, then says so", async () => {
    openWith(`#${TOKEN}`);
    const confirm = vi.fn().mockResolvedValue({ ok: true });
    renderWithIntl(<SlackConnectLanding sso={SSO} email="clerk@example.test" confirm={confirm} />);
    const button = await screen.findByRole("button", { name: "Connect my Slack account" });
    expect(confirm).not.toHaveBeenCalled();
    await userEvent.click(button);
    expect(confirm).toHaveBeenCalledWith(TOKEN);
    expect(await screen.findByText("Slack is connected")).toBeInTheDocument();
    expect(localStorage.getItem(PENDING_SLACK_KEY)).toBeNull();
  });

  it("explains a used or expired link, in Arabic too", async () => {
    openWith(`#${TOKEN}`);
    const confirm = vi.fn().mockResolvedValue({ ok: false, code: "gone" });
    renderWithIntl(<SlackConnectLanding sso={SSO} email="clerk@example.test" confirm={confirm} />, "ar");
    await userEvent.click(await screen.findByRole("button", { name: "ربط حسابي في Slack" }));
    await waitFor(() => expect(screen.getByText("هذا الرابط لم يعد يعمل")).toBeInTheDocument());
  });

  it("says the link is not valid when there is no token at all", async () => {
    openWith("");
    renderWithIntl(<SlackConnectLanding sso={SSO} email="clerk@example.test" />);
    expect(await screen.findByText("This link does not work any more")).toBeInTheDocument();
  });
});
