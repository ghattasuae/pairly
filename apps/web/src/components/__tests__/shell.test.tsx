// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
import { fireEvent, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { renderWithIntl } from "../../../test/intl";
import { navigationMock, setPath } from "../../../test/navigation-mock";

vi.mock("@/i18n/navigation", () => navigationMock);
vi.mock("@/lib/auth/client", () => ({ authClient: {} }));

const { visibleNav } = await import("@/lib/nav");
const { Sidebar } = await import("../shell/sidebar");
const { ActionNeeded } = await import("../states/action-needed");
const { NotProvisioned } = await import("../states/not-provisioned");
const { SignInForm } = await import("../auth/sign-in-form");
const { readTokenFromHash, verifyHref } = await import("../auth/magic-link-confirm");
const { TooltipProvider } = await import("../ui/tooltip");

describe("Sidebar", () => {
  beforeEach(() => setPath("/pay"));

  it("renders only the sections it is given and marks the current one", () => {
    renderWithIntl(
      <TooltipProvider>
        <Sidebar items={visibleNav(["pay.invoice.read"])} />
      </TooltipProvider>,
    );
    const nav = screen.getByRole("navigation", { name: "Main" });
    const links = Array.from(nav.querySelectorAll("a")).map((a) => a.textContent);
    expect(links).toEqual(["Home", "Inbox", "Pay", "Settings"]);
    expect(screen.getByRole("link", { name: "Pay" })).toHaveAttribute("aria-current", "page");
    expect(screen.getByRole("link", { name: "Home" })).not.toHaveAttribute("aria-current");
  });

  it("uses Arabic labels in the ar locale", () => {
    renderWithIntl(
      <TooltipProvider>
        <Sidebar items={visibleNav(null)} />
      </TooltipProvider>,
      "ar",
    );
    expect(screen.getByRole("link", { name: "الرئيسية" })).toBeInTheDocument();
  });
});

describe("ActionNeeded", () => {
  it("shows the calm empty state when nothing is waiting", () => {
    renderWithIntl(<ActionNeeded count={0} />);
    expect(screen.getByRole("heading", { name: "Action needed" })).toBeInTheDocument();
    expect(screen.getByText("Nothing needs you right now.")).toBeInTheDocument();
  });
});

describe("NotProvisioned", () => {
  it("names the signed-in email and the reference code", () => {
    renderWithIntl(<NotProvisioned email="sam@example.test" code="forbidden" />);
    expect(screen.getByRole("heading", { name: "Your account is not set up yet" })).toBeInTheDocument();
    expect(screen.getByText(/sam@example\.test/)).toBeInTheDocument();
    expect(screen.getByText("Reference: forbidden")).toBeInTheDocument();
  });
});

describe("SignInForm", () => {
  it("rejects an invalid email without calling the server", async () => {
    const sendLink = vi.fn();
    renderWithIntl(<SignInForm sso={{ google: false, microsoft: false }} sendLink={sendLink} />);
    await userEvent.type(screen.getByLabelText("Work email"), "not-an-email");
    fireEvent.submit(screen.getByRole("button", { name: "Email me a link" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Enter a valid email address.");
    expect(sendLink).not.toHaveBeenCalled();
  });

  it("sends the link with the current locale and shows rate limits plainly", async () => {
    const sendLink = vi.fn().mockResolvedValue({ ok: false, status: 429 });
    renderWithIntl(<SignInForm sso={{ google: false, microsoft: false }} sendLink={sendLink} />, "ar");
    await userEvent.type(screen.getByLabelText("بريد العمل"), "sam@example.test");
    await userEvent.click(screen.getByRole("button", { name: "أرسل لي رابطًا" }));
    expect(sendLink).toHaveBeenCalledWith("sam@example.test", "ar", null);
    expect(await screen.findByRole("alert")).toHaveTextContent("محاولات كثيرة");
  });

  it("hides SSO buttons unless configured", () => {
    renderWithIntl(<SignInForm sso={{ google: true, microsoft: false }} />);
    expect(screen.getByRole("button", { name: "Continue with Google" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Continue with Microsoft" })).toBeNull();
  });
});

describe("magic link confirm", () => {
  it("only accepts a well-formed token from the fragment", () => {
    expect(readTokenFromHash("#token=abcdefghijklmnopQRSTUV")).toBe("abcdefghijklmnopQRSTUV");
    expect(readTokenFromHash("#token=short")).toBeNull();
    expect(readTokenFromHash("#token=bad%22token%3Cscript%3Eabcdefg")).toBeNull();
    expect(readTokenFromHash("")).toBeNull();
  });

  it("builds a same-origin verify link with fixed callbacks", () => {
    const href = verifyHref("abcdefghijklmnopQRSTUV", "ar");
    const url = new URL(href, "http://localhost:3000");
    expect(url.pathname).toBe("/api/auth/magic-link/verify");
    expect(url.searchParams.get("callbackURL")).toBe("/ar");
    expect(url.searchParams.get("errorCallbackURL")).toBe("/ar/sign-in");
  });
});
