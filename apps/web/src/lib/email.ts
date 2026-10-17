// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology

/**
 * Transactional email for auth flows (magic links, invitations). SMTP (Mailpit for the
 * Docker stack, any SMTP relay in production) or, for the native development stack and
 * tests only, the file outbox (`outbox.ts`, `PAIRLY_EMAIL_TRANSPORT=outbox`). Bodies are built from the locale's message file,
 * plain text plus a minimal HTML version with every interpolated value escaped.
 *
 * Invitation emails are sent here, by the web server, right after the API returns the new
 * invitation's token: the raw token then never has to be stored anywhere (a worker job
 * would have to put it in the queue), and the link points at this app.
 */
import nodemailer, { type Transporter } from "nodemailer";
import { createTranslator } from "next-intl";

import { loadMessages } from "@/i18n/messages";
import { type Locale, localeDirection } from "@/i18n/routing";

import { serverEnv } from "./env";
import { writeOutboxMessage } from "./outbox";

let transporter: Transporter | undefined;

async function deliver(mail: { to: string; subject: string; text: string; html: string }): Promise<void> {
  const env = serverEnv();
  // The hosted demo: nothing leaves the server (lib/env.ts refuses it in production).
  if (env.email.transport === "fake") return;
  const message = { from: env.smtp.from, ...mail };
  if (env.email.transport === "outbox" && env.email.outboxDir) {
    await writeOutboxMessage(env.email.outboxDir, message);
    return;
  }
  await getTransporter().sendMail(message);
}

function getTransporter(): Transporter {
  if (transporter) return transporter;
  const { smtp } = serverEnv();
  transporter = nodemailer.createTransport({
    host: smtp.host,
    port: smtp.port,
    secure: smtp.secure,
    ...(smtp.user && smtp.password ? { auth: { user: smtp.user, pass: smtp.password } } : {}),
    requireTLS: smtp.requireTls,
  });
  return transporter;
}

export function escapeHtml(value: string): string {
  return value
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function button(url: string, label: string): string {
  return `<p><a href="${escapeHtml(url)}" style="display:inline-block;padding:10px 16px;border-radius:8px;background:#1d5c52;color:#ffffff;text-decoration:none">${escapeHtml(label)}</a></p>`;
}

/** Days an invitation stays valid. Must match the API (`INVITATION_TTL`). */
export const INVITATION_DAYS = 7;

export async function sendInvitationEmail(params: {
  to: string;
  url: string;
  locale: Locale;
  orgName: string;
  role: string;
  inviter: string;
}): Promise<void> {
  const messages = await loadMessages(params.locale);
  const t = createTranslator({ locale: params.locale, messages, namespace: "Email.invitation" });
  const roles = createTranslator({ locale: params.locale, messages, namespace: "Roles" });
  const role = roles.has(params.role as "admin") ? roles(params.role as "admin") : params.role;
  const values = { org: params.orgName, inviter: params.inviter, role };
  const subject = t("subject", values);
  const text = [
    t("greeting"),
    "",
    t("body", values),
    "",
    params.url,
    "",
    t("expires", { days: INVITATION_DAYS }),
    t("ignore"),
  ].join("\n");
  const dir = localeDirection[params.locale];
  const html = `<!doctype html><html lang="${params.locale}" dir="${dir}"><body style="font-family:system-ui,sans-serif;line-height:1.5;color:#1f2a2a">
<p>${escapeHtml(t("greeting"))}</p>
<p>${escapeHtml(t("body", values))}</p>
${button(params.url, t("button"))}
<p style="color:#5b6766;font-size:14px">${escapeHtml(t("expires", { days: INVITATION_DAYS }))}<br>${escapeHtml(t("ignore"))}</p>
</body></html>`;
  await deliver({ to: params.to, subject, text, html });
}

export async function sendMagicLinkEmail(params: {
  to: string;
  url: string;
  locale: Locale;
}): Promise<void> {
  const messages = await loadMessages(params.locale);
  const t = createTranslator({ locale: params.locale, messages, namespace: "Email.magicLink" });
  const minutes = 5;
  const subject = t("subject");
  const text = [
    t("greeting"),
    "",
    t("body"),
    "",
    params.url,
    "",
    t("expires", { minutes }),
    t("ignore"),
  ].join("\n");
  const dir = localeDirection[params.locale];
  const html = `<!doctype html><html lang="${params.locale}" dir="${dir}"><body style="font-family:system-ui,sans-serif;line-height:1.5;color:#1f2a2a">
<p>${escapeHtml(t("greeting"))}</p>
<p>${escapeHtml(t("body"))}</p>
<p><a href="${escapeHtml(params.url)}" style="display:inline-block;padding:10px 16px;border-radius:8px;background:#1d5c52;color:#ffffff;text-decoration:none">${escapeHtml(t("button"))}</a></p>
<p style="color:#5b6766;font-size:14px">${escapeHtml(t("expires", { minutes }))}<br>${escapeHtml(t("ignore"))}</p>
</body></html>`;
  await deliver({ to: params.to, subject, text, html });
}
