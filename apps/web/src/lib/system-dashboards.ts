// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology

/**
 * Shared and system dashboards on Home (brief 5.1 items 12 to 15): which a person gets
 * from their roles and permissions, and small pure helpers for showing them. The API
 * decides what each holds and checks every permission again.
 *
 * Platform operations is only for admins of the deployment's system organisation: the
 * web app cannot tell which organisation that is, so it asks for admins and leaves the
 * board out quietly when the API answers 404.
 */
import type { components } from "@pairly/shared-ts";

type S = components["schemas"];

export type SystemDashboard = S["SysDashboardOut"];
export type SysSection = S["SysDashSectionOut"];
export type SysItem = S["SysDashItemOut"];
export type SysStat = S["SysDashStatOut"];
export type OpsJobRun = S["OpsJobRunOut"];

export const SYSTEM_DASHBOARDS = ["auditor", "admin", "ai_governance", "platform_ops"] as const;
export type SystemDashboardKey = (typeof SYSTEM_DASHBOARDS)[number];

export const SYSTEM_DASHBOARD_PATHS = {
  auditor: "/v1/dashboards/auditor",
  admin: "/v1/dashboards/admin",
  ai_governance: "/v1/dashboards/ai-governance",
  platform_ops: "/v1/ops/dashboard",
} as const satisfies Record<SystemDashboardKey, string>;

/** The permission the API checks for each one (display only). */
const NEEDS: Record<SystemDashboardKey, string> = {
  auditor: "core.audit.read",
  admin: "core.admin_dashboard.read",
  ai_governance: "core.ai_governance.read",
  platform_ops: "core.platform.read",
};

/** Which organisation-wide role opens which board. */
const BY_ROLE: Record<string, readonly SystemDashboardKey[]> = {
  auditor: ["auditor", "ai_governance"],
  admin: ["admin", "ai_governance", "platform_ops"],
};

export interface SysRoleRef {
  role: string;
  workflow?: string | null;
}

/** The boards for these roles, in a stable order. Only memberships that cover every
 * workflow count (the boards are organisation-wide), and only what the permissions allow. */
export function systemDashboardsFor(roles: readonly SysRoleRef[], permissions: readonly string[]): SystemDashboardKey[] {
  const wanted = new Set<SystemDashboardKey>();
  for (const r of roles) {
    if (r.workflow) continue;
    for (const key of BY_ROLE[r.role] ?? []) wanted.add(key);
  }
  return SYSTEM_DASHBOARDS.filter((d) => wanted.has(d) && permissions.includes(NEEDS[d]));
}

/** A board the API says does not exist for this person (platform operations outside the
 * system organisation) is left out without the "could not load" note. */
export function quietlyAbsent(key: SystemDashboardKey, status: number | null): boolean {
  return key === "platform_ops" && status === 404;
}

/** How many things need this person now, across these boards (unknown counts are not 0). */
export function systemActionTotal(boards: readonly SystemDashboard[]): number {
  return boards.reduce((sum, b) => sum + b.action.reduce((n, s) => n + (s.count ?? 0), 0), 0);
}

/** Only paths inside the app are followed (the API sends app paths). */
export function safeHref(href: string | null | undefined): string | null {
  return href && href.startsWith("/") && !href.startsWith("//") ? href : null;
}

export type StatValue =
  | { kind: "none" }
  | { kind: "text"; text: string }
  | { kind: "number"; value: number; unit: "count" | "ratio" | "usd" | "ms" | "bytes" | "seconds" };

/** What a stat shows: no data, a text, or a number with its unit. Never a made-up 0. */
export function statValue(stat: SysStat): StatValue {
  if (stat.unit === "text") return stat.text ? { kind: "text", text: stat.text } : { kind: "none" };
  if (stat.value === null || stat.value === undefined || Number.isNaN(stat.value)) return { kind: "none" };
  const unit = (["count", "ratio", "usd", "ms", "bytes", "seconds"] as const).find((u) => u === stat.unit) ?? "count";
  return { kind: "number", value: stat.value, unit };
}

/** Bytes in the largest unit that keeps the number at or above 1 (binary units). */
export function formatBytes(bytes: number): string {
  const units = ["B", "KiB", "MiB", "GiB", "TiB"];
  let value = bytes;
  let index = 0;
  while (value >= 1024 && index < units.length - 1) {
    value /= 1024;
    index += 1;
  }
  return `${index === 0 ? value : value.toFixed(1)} ${units[index]}`;
}

/** The detail entries worth showing, in the API's order (empty values left out). */
export function detailEntries(detail: SysItem["detail"]): [string, string | number | boolean][] {
  return Object.entries(detail).flatMap(([key, value]) =>
    value === null || value === undefined || value === "" ? [] : [[key, value] as [string, string | number | boolean]],
  );
}
