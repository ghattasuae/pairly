// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
"use client";

import { useEffect, useState } from "react";

import { describeInvitation } from "@/lib/actions/invitations";
import { readInviteToken, type InviteView } from "@/lib/invite-view";

import { InvitePanel } from "./invite-panel";

/** localStorage key for the token while the person signs in (a magic link opens a new tab). */
export const PENDING_INVITE_KEY = "pairly.pendingInvite";
/** How long a stored token is kept. Well under the token's own 7 day life. */
export const PENDING_INVITE_MS = 60 * 60 * 1000;

interface Stored {
  token: string;
  at: number;
}

function load(now: number): string | null {
  try {
    const raw = localStorage.getItem(PENDING_INVITE_KEY);
    const parsed = raw ? (JSON.parse(raw) as Partial<Stored>) : null;
    if (!parsed || typeof parsed.token !== "string" || typeof parsed.at !== "number") return null;
    if (now - parsed.at > PENDING_INVITE_MS) return null;
    return readInviteToken(`#token=${parsed.token}`);
  } catch {
    return null;
  }
}

function save(token: string, now: number): void {
  try {
    localStorage.setItem(PENDING_INVITE_KEY, JSON.stringify({ token, at: now } satisfies Stored));
  } catch {
    /* storage blocked: the fragment still works in this tab */
  }
}

export function forgetPendingInvite(): void {
  try {
    localStorage.removeItem(PENDING_INVITE_KEY);
  } catch {
    /* nothing to forget */
  }
}

/**
 * The invite page. The token arrives in the URL fragment (`/<locale>/invite#token=...`),
 * which browsers never send to a server. This component reads it, keeps it in this
 * browser for an hour so the person can come back after the magic link, removes it from
 * the address bar, and asks the server what it invites to.
 */
export function InviteLanding({
  describe = describeInvitation,
}: {
  /** Override for tests. Defaults to the server action. */
  describe?: (token: string) => Promise<InviteView>;
}) {
  const [state, setState] = useState<{ token: string; view: InviteView } | null>(null);

  useEffect(() => {
    const now = Date.now();
    const fromHash = readInviteToken(window.location.hash);
    if (fromHash) {
      save(fromHash, now);
      window.history.replaceState(null, "", window.location.pathname);
    }
    const token = fromHash ?? load(now);
    let cancelled = false;
    const found: Promise<InviteView> = token ? describe(token) : Promise.resolve({ kind: "invalid" });
    found
      .then((view) => {
        if (view.kind === "invalid") forgetPendingInvite();
        if (!cancelled) setState({ token: token ?? "", view });
      })
      .catch(() => {
        if (!cancelled) setState({ token: token ?? "", view: { kind: "unavailable" } });
      });
    return () => {
      cancelled = true;
    };
  }, [describe]);

  if (state === null) return null;
  return <InvitePanel token={state.token} view={state.view} onAccepted={forgetPendingInvite} />;
}
