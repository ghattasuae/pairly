// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology

// Minimal Mailpit HTTP API client for the e2e tests (read-only: we only ever read mail
// that the app under test just sent, to pull out magic-link / invite tokens).

import { BASE_URL, EMAILED_HOST } from "./origin";

export const MAILPIT_URL = process.env.MAILPIT_URL ?? "http://127.0.0.1:8025";

interface MailpitMessageSummary {
  ID: string;
  To: { Address: string; Name: string }[];
  Created: string;
}

interface MailpitMessagesResponse {
  messages: MailpitMessageSummary[];
}

interface MailpitMessage {
  ID: string;
  HTML: string;
  Text: string;
}

/** What we navigate to (utils/origin.ts). By default the emailed host itself. */
const REACHABLE_HOST = new URL(BASE_URL).host;

/** Rewrite an emailed absolute URL so Playwright hits the right server (a no-op by
 *  default, when the suite opens the emailed host). */
export function toReachableUrl(url: string): string {
  return url.replace(EMAILED_HOST, REACHABLE_HOST);
}

async function mailpitFetch<T>(path: string): Promise<T> {
  const res = await fetch(`${MAILPIT_URL}${path}`);
  if (!res.ok) throw new Error(`Mailpit ${path} -> ${res.status}`);
  return (await res.json()) as T;
}

/** Delete everything in Mailpit (call before triggering an email, to avoid stale matches). */
export async function clearMailbox(): Promise<void> {
  await fetch(`${MAILPIT_URL}/api/v1/messages`, { method: "DELETE" });
}

/**
 * Poll Mailpit until a message to `address` shows up (newest first), then return its
 * full HTML body. Throws after `timeoutMs`.
 */
export async function waitForMessageTo(address: string, timeoutMs = 20_000): Promise<MailpitMessage> {
  const deadline = Date.now() + timeoutMs;
  const target = address.toLowerCase();
  while (Date.now() < deadline) {
    const list = await mailpitFetch<MailpitMessagesResponse>("/api/v1/messages?limit=50");
    const match = list.messages.find((m) => m.To.some((to) => to.Address.toLowerCase() === target));
    if (match) return mailpitFetch<MailpitMessage>(`/api/v1/message/${match.ID}`);
    await new Promise((r) => setTimeout(r, 500));
  }
  throw new Error(`No Mailpit message to ${address} after ${timeoutMs}ms`);
}

/**
 * Poll Mailpit until a message to `address` whose HTML contains every one of `needles`
 * shows up (newest first), then return it. For inboxes that get several kinds of mail.
 */
export async function waitForMessageWith(
  address: string,
  needles: readonly string[],
  timeoutMs = 90_000,
): Promise<MailpitMessage> {
  const deadline = Date.now() + timeoutMs;
  const target = address.toLowerCase();
  const seen = new Set<string>();
  while (Date.now() < deadline) {
    const list = await mailpitFetch<MailpitMessagesResponse>("/api/v1/messages?limit=100");
    for (const summary of list.messages) {
      if (seen.has(summary.ID) || !summary.To.some((to) => to.Address.toLowerCase() === target)) continue;
      const message = await mailpitFetch<MailpitMessage>(`/api/v1/message/${summary.ID}`);
      if (needles.every((needle) => message.HTML.includes(needle))) return message;
      seen.add(summary.ID);
    }
    await new Promise((r) => setTimeout(r, 1000));
  }
  throw new Error(`No Mailpit message to ${address} containing ${JSON.stringify(needles)} after ${timeoutMs}ms`);
}

/** Pull the first href from an HTML email body that matches `pathPart` (e.g. "sign-in/confirm"). */
export function extractLink(html: string, pathPart: string): string {
  const re = /href="([^"]+)"/g;
  let m: RegExpExecArray | null;
  while ((m = re.exec(html))) {
    const href = m[1]!.replace(/&amp;/g, "&");
    if (href.includes(pathPart)) return href;
  }
  throw new Error(`No link containing "${pathPart}" found in email HTML:\n${html}`);
}
