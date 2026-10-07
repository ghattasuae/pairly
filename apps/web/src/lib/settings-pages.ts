// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology

/**
 * The settings pages and who sees a link to each. Display only: every page checks again
 * on the server, and the API checks every call.
 */
export type SettingsKey = "notifications" | "security" | "members" | "flags" | "apiKeys" | "webhooks" | "policies";

export interface SettingsPageLink {
  key: SettingsKey;
  href: `/settings/${string}`;
  visible: (permissions: readonly string[]) => boolean;
}

export const SETTINGS_PAGES: readonly SettingsPageLink[] = [
  { key: "notifications", href: "/settings/notifications", visible: () => true },
  { key: "security", href: "/settings/security", visible: () => true },
  { key: "members", href: "/settings/members", visible: (p) => p.includes("core.users.read") },
  { key: "policies", href: "/settings/policies", visible: (p) => p.includes("core.policy.read") },
  { key: "flags", href: "/settings/flags", visible: (p) => p.includes("core.flags.read") },
  { key: "apiKeys", href: "/settings/api-keys", visible: (p) => p.includes("core.api_keys.manage") },
  { key: "webhooks", href: "/settings/webhooks", visible: (p) => p.includes("core.webhooks.read") },
];

export function canSee(key: SettingsKey, permissions: readonly string[]): boolean {
  return SETTINGS_PAGES.some((page) => page.key === key && page.visible(permissions));
}
