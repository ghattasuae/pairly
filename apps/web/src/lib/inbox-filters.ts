// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology

/**
 * The inbox keeps its filters and the open item in the URL (`?status=pending&item=...`),
 * so a reload, the back button or a shared link lands in the same place. Pure, no I/O.
 */

export const STATUSES = ["pending", "approved", "rejected", "changes_requested", "cancelled", "all"] as const;
export const SLA_STATES = ["on_track", "due_soon", "overdue"] as const;

export type InboxStatus = (typeof STATUSES)[number];
export type InboxSla = (typeof SLA_STATES)[number];

export interface InboxFilters {
  workflow: string | null;
  status: InboxStatus;
  sla: InboxSla | null;
  mine: boolean;
  /** Text typed into the list search (matches titles in the loaded list). */
  q: string;
}

export const DEFAULT_FILTERS: InboxFilters = { workflow: null, status: "pending", sla: null, mine: false, q: "" };

const WORKFLOW = /^[a-z][a-z0-9_]{0,31}$/;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

type Params = URLSearchParams | Record<string, string | string[] | undefined>;

function get(params: Params, key: string): string | undefined {
  if (params instanceof URLSearchParams) return params.get(key) ?? undefined;
  const value = params[key];
  return Array.isArray(value) ? value[0] : value;
}

export function isUuid(value: unknown): value is string {
  return typeof value === "string" && UUID.test(value);
}

export function parseFilters(params: Params): InboxFilters {
  const workflow = get(params, "workflow");
  const status = get(params, "status");
  const sla = get(params, "sla");
  const q = get(params, "q") ?? "";
  return {
    workflow: workflow && WORKFLOW.test(workflow) ? workflow : null,
    status: STATUSES.includes(status as InboxStatus) ? (status as InboxStatus) : "pending",
    sla: SLA_STATES.includes(sla as InboxSla) ? (sla as InboxSla) : null,
    mine: get(params, "mine") === "1",
    q: q.slice(0, 100),
  };
}

/** The selected approval id from the URL, if it is a UUID. */
export function parseItem(params: Params): string | null {
  const item = get(params, "item");
  return isUuid(item) ? item.toLowerCase() : null;
}

/** The query string for filters plus the open item. Defaults are left out. */
export function toQuery(filters: InboxFilters, item: string | null): string {
  const params = new URLSearchParams();
  if (filters.workflow) params.set("workflow", filters.workflow);
  if (filters.status !== "pending") params.set("status", filters.status);
  if (filters.sla) params.set("sla", filters.sla);
  if (filters.mine) params.set("mine", "1");
  if (filters.q) params.set("q", filters.q);
  if (item) params.set("item", item);
  const query = params.toString();
  return query ? `?${query}` : "";
}

/** Query parameters for `GET /v1/approvals`. */
export function apiQuery(filters: InboxFilters, cursor: string | null, limit = 50) {
  return {
    ...(filters.workflow ? { workflow: filters.workflow } : {}),
    ...(filters.status !== "all" ? { status: filters.status } : {}),
    ...(filters.sla ? { sla: filters.sla } : {}),
    ...(filters.mine ? { assigned_to_me: true } : {}),
    ...(cursor ? { cursor } : {}),
    limit,
  };
}

/** Workflows the actor approves in, from their permissions (`pay.invoice.approve` gives pay). */
export function approvalWorkflows(permissions: readonly string[]): string[] {
  const found = new Set<string>();
  for (const permission of permissions) {
    const [ns, , action] = permission.split(".");
    if (action === "approve" && ns && ns !== "core") found.add(ns);
  }
  return [...found].sort();
}

/** Decisions that need a written reason (the API refuses them without one). */
export function needsReason(decision: "approve" | "reject" | "request_changes"): boolean {
  return decision !== "approve";
}

/** Decisions confirmed in a dialog first: external and cannot be undone. */
export function needsConfirm(decision: "approve" | "reject" | "request_changes"): boolean {
  return decision === "approve" || decision === "reject";
}

/** A fresh `Idempotency-Key` for one decision attempt (retries reuse it). */
export function newIdempotencyKey(): string {
  return `web:${crypto.randomUUID()}`;
}
