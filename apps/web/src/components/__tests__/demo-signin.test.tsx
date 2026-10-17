// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
import { screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { renderWithIntl } from "../../../test/intl";
import { DemoSigninList } from "../auth/demo-signin-list";

const ACCOUNTS = [
  { email: "demo-hire-recruiter@example.test", label: "recruiter", role: "recruiter", workflow: "hire" },
  { email: "demo-pay-clerk@example.test", label: "ap_clerk", role: "ap_clerk", workflow: "pay" },
];

describe("DemoSigninList", () => {
  it("posts one form per offered demo user to the demo sign-in route", () => {
    renderWithIntl(<DemoSigninList accounts={ACCOUNTS} locale="en" />);
    const items = screen.getAllByRole("listitem");
    expect(items).toHaveLength(2);
    const form = items[0]!.querySelector("form")!;
    expect(form.getAttribute("method")).toBe("post");
    expect(form.getAttribute("action")).toBe("/api/demo/signin");
    expect((form.querySelector('input[name="email"]') as HTMLInputElement).value).toBe(ACCOUNTS[0]!.email);
    expect((form.querySelector('input[name="locale"]') as HTMLInputElement).value).toBe("en");
    expect(within(items[0]!).getByText("Recruiter (Hire)")).toBeInTheDocument();
    expect(within(items[1]!).getByText("Accounts payable clerk (Pay)")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("shows a known error", () => {
    renderWithIntl(<DemoSigninList accounts={ACCOUNTS} locale="en" error="rate_limited" />);
    expect(screen.getByRole("alert")).toHaveTextContent("Too many sign-ins");
  });
});
