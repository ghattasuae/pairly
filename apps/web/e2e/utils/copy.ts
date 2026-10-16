// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology

import type { Page } from "@playwright/test";

import { logCopy } from "./report";

const KNOWN_NAMESPACES = [
  "Auth",
  "CheckEmail",
  "Confirm",
  "Invite",
  "TwoFactor",
  "Members",
  "Nav",
  "Shell",
  "Command",
  "Common",
  "Home",
  "Roles",
  "Workflows",
  "Locale",
  "Meta",
  "NotFound",
  "Inbox",
  "ApprovalLink",
  "ApprovalStatus",
  "Sla",
  "StepUp",
  "Errors",
  "Data",
  "Severity",
  "Explain",
  "Viewer",
  "Decision",
  "DecisionKinds",
  "Audit",
  "Notifications",
  "NotificationSettings",
  "Settings",
  "SettingsCommon",
  "FlagsSettings",
  "ApiKeysSettings",
  "WebhooksSettings",
  "PoliciesSettings",
];
const RAW_KEY_RE = new RegExp(`\\b(?:${KNOWN_NAMESPACES.join("|")})\\.[a-zA-Z][a-zA-Z0-9.]*\\b`, "g");

// Words that are fine in Arabic copy as-is (brand names, acronyms, loanwords the design
// system intentionally keeps in Latin script) so the English-run heuristic below does not
// flag every page for using them.
const ALLOWLIST = new Set([
  "pairly",
  "mailpit",
  "totp",
  "sso",
  "google",
  "microsoft",
  "api",
  "smtp",
  "url",
  "ok",
  "id",
]);

function stripAllowlisted(text: string): string {
  return text
    .split(/\s+/)
    .map((w) => (ALLOWLIST.has(w.toLowerCase().replace(/[^a-z]/gi, "")) ? "_" : w))
    .join(" ");
}

/** 4+ consecutive Latin words in a row: likely untranslated English leaking onto an ar page. */
function findSuspiciousEnglishRuns(text: string): string[] {
  const cleaned = stripAllowlisted(text);
  const matches = cleaned.match(/[A-Za-z]{2,}(?:\s+[A-Za-z]{2,}){3,}/g) ?? [];
  return [...new Set(matches.map((m) => m.trim()))].slice(0, 10);
}

export async function auditCopy(page: Page, pageName: string, locale: string, theme: string): Promise<void> {
  const text = (await page.locator("body").innerText().catch(() => "")) || "";
  const emDash = text.includes("\u2014");
  const rawKeys = [...new Set((text.match(RAW_KEY_RE) ?? []).map((s) => s))];
  const suspiciousEnglish = locale === "ar" ? findSuspiciousEnglishRuns(text) : [];
  logCopy({ page: pageName, locale, theme, emDash, rawKeys, suspiciousEnglish });
}
