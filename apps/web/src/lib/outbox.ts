// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology

/**
 * Development outbox: auth emails written to files instead of sent (native stack, no
 * Mailpit). Selected with `PAIRLY_EMAIL_TRANSPORT=outbox` and `PAIRLY_OUTBOX_DIR`; `env.ts`
 * refuses it unless `PAIRLY_ENV` is development or test.
 *
 * Same format (version 1) as the API and worker (`pairly_core.notifications.outbox`), in
 * the same folder: `<id>.eml` (the message nodemailer would have sent, built by its stream
 * transport), `<id>.json` (metadata plus text and HTML) and one line appended to
 * `index.jsonl`, last, so a reader that follows the index never sees half a message.
 */
import { randomBytes } from "node:crypto";
import { mkdir, open, rename, rm, writeFile } from "node:fs/promises";
import path from "node:path";

import nodemailer, { type Transporter } from "nodemailer";

export const OUTBOX_FORMAT_VERSION = 1;
export const OUTBOX_INDEX = "index.jsonl";

let streamer: Transporter | undefined;

function newId(): string {
  return `${String(Date.now()).padStart(13, "0")}-${randomBytes(8).toString("hex")}`;
}

async function writeAtomic(file: string, data: string | Buffer): Promise<void> {
  const tmp = path.join(path.dirname(file), `.${path.basename(file)}.${randomBytes(6).toString("hex")}.tmp`);
  try {
    await writeFile(tmp, data, { flag: "wx", mode: 0o600 });
    await rename(tmp, file);
  } finally {
    await rm(tmp, { force: true });
  }
}

export async function writeOutboxMessage(
  dir: string,
  mail: { from: string; to: string; subject: string; text: string; html: string },
): Promise<string> {
  streamer ??= nodemailer.createTransport({ streamTransport: true, buffer: true, newline: "unix" });
  const info = (await streamer.sendMail(mail)) as { message: Buffer };
  await mkdir(dir, { recursive: true });
  const id = newId();
  const entry = {
    v: OUTBOX_FORMAT_VERSION,
    id,
    created: new Date().toISOString(),
    source: "web",
    from: mail.from,
    to: [mail.to],
    subject: mail.subject,
    eml: `${id}.eml`,
  };
  await writeAtomic(path.join(dir, `${id}.eml`), info.message);
  await writeAtomic(path.join(dir, `${id}.json`), JSON.stringify({ ...entry, text: mail.text, html: mail.html }));
  const index = await open(path.join(dir, OUTBOX_INDEX), "a", 0o600);
  try {
    await index.write(`${JSON.stringify(entry)}\n`);
  } finally {
    await index.close();
  }
  return id;
}
