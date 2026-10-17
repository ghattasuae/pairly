// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { renderWithIntl } from "../../../test/intl";
import { navigationMock, routerSpies } from "../../../test/navigation-mock";

vi.mock("@/i18n/navigation", () => navigationMock);
vi.mock("next/navigation", () => ({ useRouter: () => routerSpies }));
vi.mock("@/lib/auth/client", () => ({ authClient: { signOut: vi.fn() } }));
// Server actions are replaced by props in every test; keep their modules out of jsdom.
vi.mock("@/lib/actions/invitations", () => ({
  inviteMember: vi.fn(),
  revokeInvitation: vi.fn(),
  prepareInviteSignIn: vi.fn(),
  acceptInvitation: vi.fn(),
  describeInvitation: vi.fn(),
}));
vi.mock("@/lib/actions/orgs", () => ({ switchOrganization: vi.fn() }));

const { InvitePanel } = await import("../invite/invite-panel");
const { InviteLanding, PENDING_INVITE_KEY, PENDING_INVITE_MS } = await import("../invite/invite-landing");
const { MembersPanel } = await import("../members/members-panel");
const { SecondFactorNeeded } = await import("../states/second-factor-needed");
const { NotProvisioned } = await import("../states/not-provisioned");
const { BackupCodeNotice } = await import("../states/backup-code-notice");
const { OrgSwitcher } = await import("../shell/org-switcher");

const TOKEN = "AbCdEfGhIjKlMnOpQrStUvWxYz0123456789-_abcde";
const preview = {
  org_name: "Acme",
  email: "sam@example.test",
  role: "approver",
  workflow: "pay",
  expires_at: "2026-10-14T10:00:00Z",
};

beforeEach(() => {
  vi.stubGlobal("location", { ...window.location, reload: vi.fn(), pathname: "/en/invite/x" });
  routerSpies.push.mockClear();
  routerSpies.replace.mockClear();
  routerSpies.refresh.mockClear();
});
afterEach(() => vi.unstubAllGlobals());

describe("InvitePanel", () => {
  it("explains a dead invitation without saying why", () => {
    renderWithIntl(<InvitePanel token={TOKEN} view={{ kind: "invalid" }} />);
    expect(screen.getByRole("heading", { name: "This invitation cannot be used" })).toBeInTheDocument();
    expect(screen.getByText(/Ask your admin to send a new one/)).toBeInTheDocument();
  });

  it("signs the invitee in by magic link and comes back to the invitation", async () => {
    const prepare = vi.fn().mockResolvedValue({ ok: true, email: "sam@example.test" });
    const sendLink = vi.fn().mockResolvedValue({ ok: true });
    renderWithIntl(<InvitePanel token={TOKEN} view={{ kind: "signIn", preview }} prepare={prepare} sendLink={sendLink} />);
    expect(screen.getByRole("heading", { name: "Join Acme" })).toBeInTheDocument();
    expect(screen.getByText(/as Approver/)).toBeInTheDocument();
    expect(screen.getByText("Only for Pay.")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Email me a sign-in link" }));
    expect(prepare).toHaveBeenCalledWith(TOKEN);
    // The callback has no token: it stays in the fragment and this browser only.
    expect(sendLink).toHaveBeenCalledWith("sam@example.test", "en", "/en/invite");
    await waitFor(() => expect(routerSpies.push).toHaveBeenCalledWith("/check-email"));
  });

  it("shows a plain error when the link cannot be sent", async () => {
    const prepare = vi.fn().mockResolvedValue({ ok: true, email: "sam@example.test" });
    const sendLink = vi.fn().mockResolvedValue({ ok: false, status: 429 });
    renderWithIntl(<InvitePanel token={TOKEN} view={{ kind: "signIn", preview }} prepare={prepare} sendLink={sendLink} />);
    await userEvent.click(screen.getByRole("button", { name: "Email me a sign-in link" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Too many attempts");
    expect(routerSpies.push).not.toHaveBeenCalled();
  });

  it("accepts and lands on the new organisation", async () => {
    const accept = vi.fn().mockResolvedValue({ ok: true, orgName: "Acme" });
    renderWithIntl(
      <InvitePanel token={TOKEN} view={{ kind: "accept", preview, currentEmail: "sam@example.test" }} accept={accept} />,
    );
    await userEvent.click(screen.getByRole("button", { name: "Accept and join" }));
    expect(accept).toHaveBeenCalledWith(TOKEN);
    await waitFor(() => expect(routerSpies.replace).toHaveBeenCalledWith("/"));
    expect(routerSpies.refresh).toHaveBeenCalled();
  });

  it("maps API refusals to plain messages", async () => {
    const accept = vi.fn().mockResolvedValue({ ok: false, code: "invitation_email_mismatch" });
    renderWithIntl(
      <InvitePanel token={TOKEN} view={{ kind: "accept", preview, currentEmail: "sam@example.test" }} accept={accept} />,
    );
    await userEvent.click(screen.getByRole("button", { name: "Accept and join" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("sent to a different email address");
    expect(routerSpies.replace).not.toHaveBeenCalled();
  });

  it("asks someone signed in as another person to switch, in Arabic too", async () => {
    const signOut = vi.fn().mockResolvedValue(undefined);
    renderWithIntl(
      <InvitePanel token={TOKEN} view={{ kind: "wrongUser", preview, currentEmail: "kim@example.test" }} signOut={signOut} />,
      "ar",
    );
    expect(screen.getByRole("heading", { name: "هذه الدعوة لشخص آخر" })).toBeInTheDocument();
    expect(screen.getByText(/kim@example\.test/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "سجّل الخروج وتابع" }));
    expect(signOut).toHaveBeenCalled();
  });
});

describe("InviteLanding", () => {
  const preview2 = { ...preview, workflow: null };
  afterEach(() => localStorage.clear());

  it("reads the token from the fragment, keeps it for the sign-in trip and hides it", async () => {
    const replaceState = vi.fn();
    vi.stubGlobal("location", { ...window.location, hash: `#token=${TOKEN}`, pathname: "/en/invite" });
    vi.stubGlobal("history", { replaceState });
    const describe = vi.fn().mockResolvedValue({ kind: "signIn", preview: preview2 });
    renderWithIntl(<InviteLanding describe={describe} />);
    expect(await screen.findByRole("heading", { name: "Join Acme" })).toBeInTheDocument();
    expect(describe).toHaveBeenCalledWith(TOKEN);
    expect(replaceState).toHaveBeenCalledWith(null, "", "/en/invite");
    expect(JSON.parse(localStorage.getItem(PENDING_INVITE_KEY) ?? "{}").token).toBe(TOKEN);
  });

  it("comes back to the stored token after the magic link", async () => {
    localStorage.setItem(PENDING_INVITE_KEY, JSON.stringify({ token: TOKEN, at: Date.now() }));
    vi.stubGlobal("location", { ...window.location, hash: "", pathname: "/en/invite" });
    const describe = vi.fn().mockResolvedValue({ kind: "accept", preview: preview2, currentEmail: "sam@example.test" });
    renderWithIntl(<InviteLanding describe={describe} />);
    expect(await screen.findByRole("button", { name: "Accept and join" })).toBeInTheDocument();
    expect(describe).toHaveBeenCalledWith(TOKEN);
  });

  it("ignores an old stored token and shows the dead-invitation state", async () => {
    localStorage.setItem(PENDING_INVITE_KEY, JSON.stringify({ token: TOKEN, at: Date.now() - PENDING_INVITE_MS - 1 }));
    vi.stubGlobal("location", { ...window.location, hash: "", pathname: "/en/invite" });
    const describe = vi.fn();
    renderWithIntl(<InviteLanding describe={describe} />);
    expect(await screen.findByRole("heading", { name: "This invitation cannot be used" })).toBeInTheDocument();
    expect(describe).not.toHaveBeenCalled();
  });

  it("forgets a token the server says is invalid", async () => {
    vi.stubGlobal("location", { ...window.location, hash: `#token=${TOKEN}`, pathname: "/en/invite" });
    vi.stubGlobal("history", { replaceState: vi.fn() });
    renderWithIntl(<InviteLanding describe={vi.fn().mockResolvedValue({ kind: "invalid" })} />);
    expect(await screen.findByRole("heading", { name: "This invitation cannot be used" })).toBeInTheDocument();
    expect(localStorage.getItem(PENDING_INVITE_KEY)).toBeNull();
  });
});

const members = [
  { user_id: "u1", email: "boss@example.test", display_name: null, roles: [{ role: "admin", workflow: null }] },
  { user_id: "u2", email: "pat@example.test", display_name: "Pat", roles: [{ role: "approver", workflow: "pay" }] },
];
const invitations = [
  {
    id: "7d6f1d1e-3333-4333-8333-333333333333",
    email: "new@example.test",
    role: "recruiter",
    workflow: null,
    status: "pending" as const,
    expires_at: "2026-10-14T10:00:00Z",
  },
  {
    id: "7d6f1d1e-4444-4444-8444-444444444444",
    email: "old@example.test",
    role: "auditor",
    workflow: null,
    status: "accepted" as const,
    expires_at: "2026-10-01T10:00:00Z",
  },
];

describe("MembersPanel", () => {
  it("lists members with their roles and marks the current user", () => {
    renderWithIntl(
      <MembersPanel currentUserId="u1" members={members} invitations={invitations} canManage blockedBySecondFactor={false} />,
    );
    expect(screen.getByText("boss@example.test")).toBeInTheDocument();
    expect(screen.getByText("(You)")).toBeInTheDocument();
    expect(screen.getByText("Approver · Pay")).toBeInTheDocument();
    expect(screen.getByText("Pending")).toBeInTheDocument();
    expect(screen.getByText("Accepted")).toBeInTheDocument();
    // Only the pending invitation can be withdrawn.
    expect(screen.getAllByRole("button", { name: /Withdraw the invitation/ })).toHaveLength(1);
  });

  it("sends an invitation with the chosen role, scope and email language", async () => {
    const invite = vi.fn().mockResolvedValue({ ok: true, emailed: true });
    renderWithIntl(
      <MembersPanel
        currentUserId="u1"
        members={members}
        invitations={[]}
        canManage
        blockedBySecondFactor={false}
        invite={invite}
      />,
    );
    await userEvent.type(screen.getByLabelText("Email"), "Kim@Example.test");
    await userEvent.selectOptions(screen.getByLabelText("Role"), "approver");
    await userEvent.selectOptions(screen.getByLabelText("Applies to"), "pay");
    await userEvent.selectOptions(screen.getByLabelText("Email language"), "ar");
    await userEvent.click(screen.getByRole("button", { name: "Send invitation" }));
    expect(invite).toHaveBeenCalledWith({ email: "Kim@Example.test", role: "approver", workflow: "pay", locale: "ar" });
    expect(await screen.findByRole("status")).toHaveTextContent("Invitation sent to");
    expect(routerSpies.refresh).toHaveBeenCalled();
  });

  it("shows the link to share when the email could not be sent", async () => {
    const link = `http://localhost:3000/en/invite/${TOKEN}`;
    const invite = vi.fn().mockResolvedValue({ ok: true, emailed: false, link });
    renderWithIntl(
      <MembersPanel currentUserId="u1" members={members} invitations={[]} canManage blockedBySecondFactor={false} invite={invite} />,
    );
    await userEvent.type(screen.getByLabelText("Email"), "kim@example.test");
    await userEvent.click(screen.getByRole("button", { name: "Send invitation" }));
    expect(await screen.findByText(link)).toBeInTheDocument();
  });

  it.each([
    ["second_factor_required", "Sign in with two-factor authentication to invite people."],
    ["permission_denied", "You cannot give this role."],
    ["invitation_pending", "This person already has a pending invitation."],
    ["something_new", "Something went wrong. Try again."],
  ])("explains %s", async (code, message) => {
    const invite = vi.fn().mockResolvedValue({ ok: false, code });
    renderWithIntl(
      <MembersPanel currentUserId="u1" members={members} invitations={[]} canManage blockedBySecondFactor={false} invite={invite} />,
    );
    await userEvent.type(screen.getByLabelText("Email"), "kim@example.test");
    await userEvent.click(screen.getByRole("button", { name: "Send invitation" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(message);
  });

  it("rejects a bad address before calling the server", async () => {
    const invite = vi.fn();
    renderWithIntl(
      <MembersPanel currentUserId="u1" members={members} invitations={[]} canManage blockedBySecondFactor={false} invite={invite} />,
    );
    await userEvent.type(screen.getByLabelText("Email"), "nope");
    await userEvent.click(screen.getByRole("button", { name: "Send invitation" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Check the email address and role.");
    expect(invite).not.toHaveBeenCalled();
  });

  it("withdraws a pending invitation", async () => {
    const revoke = vi.fn().mockResolvedValue({ ok: true });
    renderWithIntl(
      <MembersPanel currentUserId="u1" members={members} invitations={invitations} canManage blockedBySecondFactor={false} revoke={revoke} />,
    );
    await userEvent.click(screen.getByRole("button", { name: "Withdraw the invitation for new@example.test" }));
    expect(revoke).toHaveBeenCalledWith(invitations[0]?.id);
    await waitFor(() => expect(routerSpies.refresh).toHaveBeenCalled());
  });

  it("disables inviting and withdrawing until the session has TOTP", () => {
    renderWithIntl(
      <MembersPanel currentUserId="u1" members={members} invitations={invitations} canManage blockedBySecondFactor />,
    );
    expect(screen.getByRole("button", { name: "Send invitation" })).toBeDisabled();
    expect(screen.queryByRole("button", { name: /Withdraw/ })).toBeNull();
  });

  it("is read only without core.users.manage, in Arabic", () => {
    renderWithIntl(
      <MembersPanel currentUserId="u2" members={members} invitations={invitations} canManage={false} blockedBySecondFactor={false} />,
      "ar",
    );
    expect(screen.queryByRole("button", { name: "إرسال الدعوة" })).toBeNull();
    expect(screen.getByText(/اطلب من المسؤول دعوة أشخاص/)).toBeInTheDocument();
    expect(screen.getByText("معتمِد · المدفوعات")).toBeInTheDocument();
  });
});

describe("SecondFactorNeeded", () => {
  it("sends people without TOTP to set it up", () => {
    renderWithIntl(<SecondFactorNeeded need="setup" />);
    expect(screen.getByRole("heading", { name: "Set up two-factor to continue" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Set up two-factor" })).toHaveAttribute("href", "/settings/security");
  });

  it("asks people with TOTP on to sign in again with a code", async () => {
    const onSignOut = vi.fn().mockResolvedValue(undefined);
    renderWithIntl(<SecondFactorNeeded need="reauth" onSignOut={onSignOut} />, "ar");
    expect(screen.getByRole("heading", { name: "سجّل الدخول مرة أخرى بالرمز" })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "سجّل الخروج ثم الدخول مرة أخرى" }));
    expect(onSignOut).toHaveBeenCalled();
  });
});

describe("NotProvisioned", () => {
  it("tells people to ask their admin for an invitation", () => {
    renderWithIntl(<NotProvisioned email="sam@example.test" code="forbidden" />);
    expect(screen.getByText("Ask your Pairly admin to send an invitation to this email address.")).toBeInTheDocument();
  });
});

describe("OrgSwitcher", () => {
  it("switches through the given handler and refreshes", async () => {
    const onSwitch = vi.fn().mockResolvedValue(undefined);
    renderWithIntl(
      <OrgSwitcher
        orgs={[
          { id: "o1", name: "Acme" },
          { id: "o2", name: "Globex" },
        ]}
        activeOrgId="o1"
        onSwitch={onSwitch}
      />,
    );
    await userEvent.click(screen.getByRole("button", { name: "Switch organisation" }));
    await userEvent.click(await screen.findByRole("menuitemradio", { name: "Globex" }));
    expect(onSwitch).toHaveBeenCalledWith("o2");
  });
});

describe("BackupCodeNotice", () => {
  it("says a backup code was used and how many remain", () => {
    renderWithIntl(<BackupCodeNotice remaining={3} />);
    expect(screen.getByRole("heading", { name: "You signed in with a backup code" })).toBeInTheDocument();
    expect(screen.getByText(/You have 3 backup codes left\./)).toBeInTheDocument();
  });

  it("handles none left and Arabic plurals", () => {
    renderWithIntl(<BackupCodeNotice remaining={0} />);
    expect(screen.getByText(/You have no backup codes left\./)).toBeInTheDocument();
    renderWithIntl(<BackupCodeNotice remaining={2} />, "ar");
    expect(screen.getByText(/تبقّى لديك رمزان احتياطيان/)).toBeInTheDocument();
  });
});
