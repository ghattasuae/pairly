// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology

/**
 * Transactional email for auth flows (magic links). SMTP only: Mailpit locally, any SMTP
 * relay in production. Bodies are built from the locale's message file, plain text plus
 * a minimal HTML version with every interpolated value escaped.
 */
import nodemailer, { type Transporter } from "nodemailer";
import { createTranslator } from "next-intl";

import { loadMessages } from "@/i18n/messages";
import { type Locale, localeDirection } from "@/i18n/routing";

import { serverEnv } from "./env";

let transporter: Transporter | undefined;

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
  await getTransporter().sendMail({
    from: serverEnv().smtp.from,
    to: params.to,
    subject,
    text,
    html,
  });
}
