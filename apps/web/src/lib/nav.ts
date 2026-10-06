// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology

/**
 * Which sections the sidebar shows, from the permissions `/v1/me` returned.
 *
 * Display only. Hiding a link is a courtesy, not a control: the API checks the permission
 * on every request, so a hidden page that someone opens anyway still gets nothing.
 */

export type NavKey = "home" | "inbox" | "hire" | "pay" | "audit" | "settings";

export interface NavItem {
  key: NavKey;
  href: `/${string}`;
  /** Single-key shortcut after pressing G (G then H opens Home). */
  shortcut: string;
}

export const NAV_ITEMS: readonly NavItem[] = [
  { key: "home", href: "/", shortcut: "H" },
  { key: "inbox", href: "/inbox", shortcut: "I" },
  { key: "hire", href: "/hire", shortcut: "R" },
  { key: "pay", href: "/pay", shortcut: "P" },
  { key: "audit", href: "/audit", shortcut: "A" },
  { key: "settings", href: "/settings", shortcut: "S" },
];

const inNamespace = (permissions: readonly string[], ns: string): boolean =>
  permissions.some((p) => p.startsWith(`${ns}.`));

/** Visibility rule per section. Home is always visible to a signed-in user. */
export const NAV_RULES: Record<NavKey, (permissions: readonly string[]) => boolean> = {
  home: () => true,
  // Approvals are authorised per workflow (for example pay.invoice.approve), so show the
  // inbox to anyone who can read approvals or holds any workflow permission.
  inbox: (p) => p.includes("core.approval.read") || inNamespace(p, "hire") || inNamespace(p, "pay"),
  hire: (p) => inNamespace(p, "hire"),
  pay: (p) => inNamespace(p, "pay"),
  audit: (p) => p.includes("core.audit.read"),
  settings: (p) => p.includes("core.org.manage") || p.includes("core.users.read"),
};

/** Sections to show. `null` permissions (not provisioned, API down) shows Home only. */
export function visibleNav(permissions: readonly string[] | null): NavItem[] {
  if (!permissions) return NAV_ITEMS.filter((item) => item.key === "home");
  return NAV_ITEMS.filter((item) => NAV_RULES[item.key](permissions));
}

export function isNavVisible(key: NavKey, permissions: readonly string[] | null): boolean {
  return visibleNav(permissions).some((item) => item.key === key);
}
