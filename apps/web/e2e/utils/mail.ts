// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology

// Read the mail the app under test just sent (magic links, invitations, approval emails),
// read-only. Two backends, chosen with PAIRLY_E2E_MAIL:
//
// - `outbox` (default): the native stack (`make dev`) writes every message to files in
//   `.local/mail` (PAIRLY_OUTBOX_DIR overrides it): `index.jsonl` plus `<id>.json` and
//   `<id>.eml` per message, written by both the web app (src/lib/outbox.ts) and the
//   worker (pairly_core.notifications.outbox).
// - `mailpit`: the Docker stack (`make docker-dev`), through Mailpit's HTTP API at
//   MAILPIT_URL.
import { existsSync, readdirSync, readFileSync, rmSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { BASE_URL, EMAILED_HOST } from "./origin";

const here = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(here, "../../../..");

export const MAIL_BACKEND = (process.env.PAIRLY_E2E_MAIL ?? "outbox") as "outbox" | "mailpit";
export const MAILPIT_URL = process.env.MAILPIT_URL ?? "http://127.0.0.1:8025";
export const OUTBOX_DIR = process.env.PAIRLY_OUTBOX_DIR ?? path.join(REPO_ROOT, ".local", "mail");

if (MAIL_BACKEND !== "outbox" && MAIL_BACKEND !== "mailpit") {
  throw new Error(`PAIRLY_E2E_MAIL must be outbox or mailpit, not ${JSON.stringify(MAIL_BACKEND)}`);
}

/** One message: an id, the recipients, and the bodies. */
export interface MailMessage {
  ID: string;
  To: string[];
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

// ---------------------------------------------------------------- outbox (native stack)

interface OutboxEntry {
  id: string;
  to: string[];
}

/** Index entries, newest first. A line still being written is skipped. */
function outboxEntries(): OutboxEntry[] {
  const index = path.join(OUTBOX_DIR, "index.jsonl");
  if (!existsSync(index)) return [];
  const entries: OutboxEntry[] = [];
  for (const line of readFileSync(index, "utf8").split("\n")) {
    if (!line.trim()) continue;
    try {
      const entry = JSON.parse(line) as OutboxEntry;
      if (/^[0-9]{13}-[0-9a-f]{16}$/.test(entry.id)) entries.push(entry);
    } catch {
      // Partial last line: the writer is still appending it.
    }
  }
  return entries.reverse();
}

function outboxMessage(id: string): MailMessage {
  const full = JSON.parse(readFileSync(path.join(OUTBOX_DIR, `${id}.json`), "utf8")) as {
    to: string[];
    html: string | null;
    text: string;
  };
  return { ID: id, To: full.to, HTML: full.html ?? "", Text: full.text };
}

// ---------------------------------------------------------------- Mailpit (Docker stack)

interface MailpitSummary {
  ID: string;
  To: { Address: string; Name: string }[];
}

async function mailpitFetch<T>(urlPath: string): Promise<T> {
  const res = await fetch(`${MAILPIT_URL}${urlPath}`);
  if (!res.ok) throw new Error(`Mailpit ${urlPath} -> ${res.status}`);
  return (await res.json()) as T;
}

async function mailpitSummaries(limit: number): Promise<{ id: string; to: string[] }[]> {
  const list = await mailpitFetch<{ messages: MailpitSummary[] }>(`/api/v1/messages?limit=${limit}`);
  return list.messages.map((m) => ({ id: m.ID, to: m.To.map((t) => t.Address) }));
}

async function mailpitMessage(id: string): Promise<MailMessage> {
  const m = await mailpitFetch<{ ID: string; HTML: string; Text: string; To: { Address: string }[] }>(
    `/api/v1/message/${id}`,
  );
  return { ID: m.ID, To: m.To.map((t) => t.Address), HTML: m.HTML, Text: m.Text };
}

// ---------------------------------------------------------------- shared

async function summaries(limit: number): Promise<{ id: string; to: string[] }[]> {
  if (MAIL_BACKEND === "mailpit") return mailpitSummaries(limit);
  return outboxEntries().slice(0, limit);
}

async function message(id: string): Promise<MailMessage> {
  return MAIL_BACKEND === "mailpit" ? mailpitMessage(id) : outboxMessage(id);
}

/** Delete every stored message (call before triggering an email, to avoid stale matches). */
export async function clearMailbox(): Promise<void> {
  if (MAIL_BACKEND === "mailpit") {
    await fetch(`${MAILPIT_URL}/api/v1/messages`, { method: "DELETE" });
    return;
  }
  if (!existsSync(OUTBOX_DIR)) return;
  for (const name of readdirSync(OUTBOX_DIR)) {
    if (name === "index.jsonl" || /^[0-9]{13}-[0-9a-f]{16}\.(json|eml)$/.test(name)) {
      rmSync(path.join(OUTBOX_DIR, name), { force: true });
    }
  }
}

const sentTo = (to: string[], target: string) => to.some((address) => address.toLowerCase() === target);

/**
 * Poll until a message to `address` shows up (newest first), then return it with its
 * full HTML body. Throws after `timeoutMs`.
 */
export async function waitForMessageTo(address: string, timeoutMs = 20_000): Promise<MailMessage> {
  const deadline = Date.now() + timeoutMs;
  const target = address.toLowerCase();
  while (Date.now() < deadline) {
    const match = (await summaries(50)).find((m) => sentTo(m.to, target));
    if (match) return message(match.id);
    await new Promise((r) => setTimeout(r, 500));
  }
  throw new Error(`No ${MAIL_BACKEND} message to ${address} after ${timeoutMs}ms`);
}

/**
 * Poll until a message to `address` whose HTML contains every one of `needles` shows up
 * (newest first), then return it. For inboxes that get several kinds of mail.
 */
export async function waitForMessageWith(
  address: string,
  needles: readonly string[],
  timeoutMs = 90_000,
): Promise<MailMessage> {
  const deadline = Date.now() + timeoutMs;
  const target = address.toLowerCase();
  const seen = new Set<string>();
  while (Date.now() < deadline) {
    for (const summary of await summaries(100)) {
      if (seen.has(summary.id) || !sentTo(summary.to, target)) continue;
      const found = await message(summary.id);
      if (needles.every((needle) => found.HTML.includes(needle))) return found;
      seen.add(summary.id);
    }
    await new Promise((r) => setTimeout(r, 1000));
  }
  throw new Error(`No ${MAIL_BACKEND} message to ${address} containing ${JSON.stringify(needles)} after ${timeoutMs}ms`);
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
