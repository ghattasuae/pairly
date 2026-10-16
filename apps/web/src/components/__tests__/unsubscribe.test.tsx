// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { renderWithIntl } from "../../../test/intl";
import { navigationMock } from "../../../test/navigation-mock";

vi.mock("@/i18n/navigation", () => navigationMock);
// Server actions are replaced by props; keep their module out of jsdom.
vi.mock("@/lib/actions/unsubscribe", () => ({ describeUnsubscribe: vi.fn(), confirmUnsubscribe: vi.fn() }));

const { UnsubscribeLanding } = await import("../unsubscribe/unsubscribe-landing");
const { readUnsubscribeToken } = await import("@/lib/unsubscribe");

const TOKEN = `v1.${"a".repeat(32)}.${"b".repeat(32)}.digest.1800000000.${"M".repeat(43)}`;

function openWith(hash: string) {
  const replaceState = vi.fn();
  vi.stubGlobal("location", { ...window.location, hash, pathname: "/en/unsubscribe" });
  vi.stubGlobal("history", { replaceState });
  return replaceState;
}

afterEach(() => vi.unstubAllGlobals());

describe("readUnsubscribeToken", () => {
  it("accepts only the token shape", () => {
    expect(readUnsubscribeToken(`#token=${TOKEN}`)).toBe(TOKEN);
    expect(readUnsubscribeToken("#token=v1.nope")).toBeNull();
    expect(readUnsubscribeToken("")).toBeNull();
  });
});

describe("UnsubscribeLanding", () => {
  it("says an expired link expired and offers sign-in to manage settings", async () => {
    const replaceState = openWith(`#token=${TOKEN}`);
    const describe = vi.fn().mockResolvedValue({ kind: "expired" });
    const confirm = vi.fn();
    renderWithIntl(<UnsubscribeLanding describe={describe} confirm={confirm} />);
    expect(await screen.findByRole("heading", { name: "This link has expired" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Sign in to manage email settings" })).toHaveAttribute("href", "/settings/notifications");
    expect(describe).toHaveBeenCalledWith(TOKEN);
    expect(replaceState).toHaveBeenCalledWith(null, "", "/en/unsubscribe");
    expect(confirm).not.toHaveBeenCalled();
  });

  it("asks before unsubscribing, then confirms", async () => {
    openWith(`#token=${TOKEN}`);
    const describe = vi.fn().mockResolvedValue({ kind: "valid", category: "digest" });
    const confirm = vi.fn().mockResolvedValue({ ok: true });
    renderWithIntl(<UnsubscribeLanding describe={describe} confirm={confirm} />);
    expect(await screen.findByText(/daily summary emails/)).toBeInTheDocument();
    expect(confirm).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole("button", { name: "Turn off these emails" }));
    expect(await screen.findByRole("heading", { name: "You are unsubscribed" })).toBeInTheDocument();
    expect(confirm).toHaveBeenCalledWith(TOKEN);
  });

  it("treats a missing token as an invalid link, in Arabic too", async () => {
    openWith("");
    const describe = vi.fn();
    renderWithIntl(<UnsubscribeLanding describe={describe} />, "ar");
    expect(await screen.findByRole("heading", { name: "هذا الرابط لا يعمل" })).toBeInTheDocument();
    expect(describe).not.toHaveBeenCalled();
  });
});
