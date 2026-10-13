// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology

/**
 * Role dashboards on Home (brief 5.1 items 1 to 7, 5.2 item 1): which dashboards a
 * person gets, and where each record and number links. Pure, so it is tested without a
 * browser. The API decides what each dashboard holds and checks every permission again.
 *
 * Candidates use the portal and referees their signed link: they never reach Home.
 */
import type { components } from "@pairly/shared-ts";

type S = components["schemas"];

export type Dashboard = S["HireDashboardOut"];
export type DashSection = S["HireDashSectionOut"];
export type DashItem = S["HireDashItemOut"];
export type DashMetric = S["HireMetricOut"];

export const DASHBOARDS = ["recruiter", "hiring_manager", "interviewer", "talent", "referrer"] as const;
export type DashboardKey = (typeof DASHBOARDS)[number];

/** The API path of each dashboard. */
export const DASHBOARD_PATHS = {
  recruiter: "/v1/hire/dashboards/recruiter",
  hiring_manager: "/v1/hire/dashboards/hiring-manager",
  interviewer: "/v1/hire/dashboards/interviewer",
  talent: "/v1/hire/dashboards/talent",
  referrer: "/v1/hire/dashboards/referrer",
} as const satisfies Record<DashboardKey, string>;

/** The permissions the API checks for each one (display only). */
const NEEDS: Record<DashboardKey, readonly string[]> = {
  recruiter: ["hire.candidate.read", "hire.pipeline.write"],
  hiring_manager: ["hire.candidate.read", "hire.requisition.create"],
  interviewer: ["hire.interview.read"],
  talent: ["hire.analytics.read"],
  referrer: ["hire.referral.read"],
};

/** Which role opens which dashboard: an admin is the HR or talent lead here. */
const BY_ROLE: Record<string, DashboardKey> = {
  recruiter: "recruiter",
  hiring_manager: "hiring_manager",
  interviewer: "interviewer",
  admin: "talent",
  employee: "referrer",
};

export interface RoleRef {
  role: string;
  workflow?: string | null;
}

/** The dashboards for these roles (Hire or every workflow), in a stable order, and only
 * those the permissions allow. */
export function dashboardsFor(roles: readonly RoleRef[], permissions: readonly string[]): DashboardKey[] {
  const wanted = new Set<DashboardKey>();
  for (const r of roles) {
    if (r.workflow && r.workflow !== "hire") continue;
    const key = BY_ROLE[r.role];
    if (key) wanted.add(key);
  }
  return DASHBOARDS.filter((d) => wanted.has(d) && NEEDS[d].every((p) => permissions.includes(p)));
}

/** Where a record lives in the app (null: nowhere to go, e.g. a referral). */
export function itemHref(item: DashItem): string | null {
  const job = item.job_id;
  const app = item.application_id;
  switch (item.kind) {
    case "application":
      return job && app ? `/hire/jobs/${job}/pipeline?candidate=${app}` : null;
    case "interview": {
      if (!app) return null;
      const stage = typeof item.detail.stage === "string" ? item.detail.stage : null;
      return `/hire/applications/${app}/kit${stage ? `?stage=${encodeURIComponent(stage)}` : ""}`;
    }
    case "debrief":
      return job && app ? `/hire/jobs/${job}/debrief/${app}` : null;
    case "shortlist":
      return job ? `/hire/jobs/${job}/shortlist` : null;
    case "offer":
      return app ? `/hire/applications/${app}/offer` : null;
    case "job":
      return job ? `/hire/jobs/${job}/pipeline` : "/hire";
    case "requisition":
      return "/hire/requisitions";
    case "approval":
      return `/inbox/${item.id}`;
    default:
      return null;
  }
}

/** The page holding every record behind a section's number, when there is one; else
 * the list right below it on Home (`#anchor`). */
export function sectionHref(dashboard: DashboardKey, section: DashSection): string {
  const jobs = new Set(section.items.map((i) => i.job_id).filter(Boolean));
  const onlyJob = jobs.size === 1 ? [...jobs][0] : null;
  switch (section.key) {
    case "to_review":
      return onlyJob ? `/hire/jobs/${onlyJob}/screen` : "/hire";
    case "approvals":
    case "approvals_late":
      return "/inbox";
    case "waiting":
      return "/hire";
    case "flags":
    case "sla":
      return "/hire/analytics";
    case "requisitions":
    case "requisitions_open":
      return "/hire/requisitions";
    case "shortlists":
      return onlyJob ? `/hire/jobs/${onlyJob}/shortlist` : `#${anchor(dashboard, section.key)}`;
    default:
      return `#${anchor(dashboard, section.key)}`;
  }
}

export function anchor(dashboard: DashboardKey, key: string): string {
  return `dash-${dashboard}-${key}`.replace(/_/g, "-");
}

/** The records behind a metric live on the analytics page. */
export function metricHref(metric: DashMetric): string {
  return `/hire/analytics#${(metric.key.split(".")[0] ?? "").replace(/_/g, "-")}`;
}

/** How many things need this person now, across their dashboards. */
export function actionTotal(boards: readonly Dashboard[]): number {
  return boards.reduce((sum, b) => sum + b.action.reduce((n, s) => n + s.count, 0), 0);
}

/** Pages a dashboard links to by key. */
export const PAGE_LINKS: Record<string, string> = {
  analytics: "/hire/analytics",
  bias: "/hire/bias",
  surveys: "/hire/surveys",
  calibration: "/hire/calibration",
  requisitions: "/hire/requisitions",
  inbox: "/inbox",
  referrals: "/hire",
};

/** A metric's value as people read it (null: not enough data). */
export function metricValue(metric: DashMetric): { kind: "rate" | "days" | "count" | "money" | "none"; value: number } {
  if (metric.value === null || metric.value === undefined) return { kind: "none", value: 0 };
  if (metric.unit === "rate") return { kind: "rate", value: metric.value };
  if (metric.unit === "days") return { kind: "days", value: Math.round(metric.value * 10) / 10 };
  if (metric.unit === "usd") return { kind: "money", value: metric.value };
  return { kind: "count", value: metric.value };
}
