// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology

/**
 * Hosted demo only: capture a magic link instead of emailing it (lib/demo/policy.ts).
 *
 * `captureMagicLink(email, run)` runs `run` (which asks Better Auth for a magic link)
 * with a capture slot bound to this async call chain only (AsyncLocalStorage). The
 * `sendMagicLink` callback in lib/auth/config.ts checks `captureFor(email)` first: when
 * this very request is capturing that very address, it stores the link there and sends
 * nothing. Any other request, or another address, takes the normal path (quota, email).
 */
import { AsyncLocalStorage } from "node:async_hooks";

interface Slot {
  email: string;
  link: string | null;
}

const slots = new AsyncLocalStorage<Slot>();

function normalise(email: string): string {
  return email.trim().toLowerCase();
}

export async function captureMagicLink(email: string, run: () => Promise<unknown>): Promise<string | null> {
  const slot: Slot = { email: normalise(email), link: null };
  await slots.run(slot, run);
  return slot.link;
}

/** The capture slot of this call chain when it is capturing `email`, else null. */
export function captureFor(email: string): { keep: (link: string) => void } | null {
  const slot = slots.getStore();
  if (!slot || slot.email !== normalise(email)) return null;
  return {
    keep: (link: string) => {
      slot.link = link;
    },
  };
}
