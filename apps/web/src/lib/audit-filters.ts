// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology

/**
 * Audit viewer filters, kept in the URL so a search can be shared, reloaded or exported
 * as it is. Pure, no I/O. The API matches each filter exactly; dates are whole UTC days
 * (`from` inclusive, `to` inclusive here, sent to the API as the next midnight).
 */

export const ACTOR_TYPES = ["user", "agent", "system", "api_key"] as const;
export type ActorTypeFilter = (typeof ACTOR_TYPES)[number];

export interface AuditFilters {
  action: string;
  actorType: ActorTypeFilter | "";
  actorId: string;
  entityType: string;
  entityId: string;
  workflow: string;
  requestId: string;
  /** YYYY-MM-DD, inclusive, UTC. */
  from: string;
  /** YYYY-MM-DD, inclusive, UTC. */
  to: string;
  order: "desc" | "asc";
}

export const EMPTY_AUDIT_FILTERS: AuditFilters = {
  action: "",
  actorType: "",
  actorId: "",
  entityType: "",
  entityId: "",
  workflow: "",
  requestId: "",
  from: "",
  to: "",
  order: "desc",
};

const TOKEN = /^[A-Za-z0-9_.:@-]{1,128}$/;
const WORKFLOW = /^[a-z][a-z0-9_]{0,31}$/;
const DAY = /^\d{4}-\d{2}-\d{2}$/;
const CURSOR = /^[A-Za-z0-9_-]{1,64}$/;

type Params = URLSearchParams | Record<string, string | string[] | undefined>;

function get(params: Params, key: string): string {
  if (params instanceof URLSearchParams) return params.get(key) ?? "";
  const value = params[key];
  return (Array.isArray(value) ? value[0] : value) ?? "";
}

function validDay(value: string): string {
  if (!DAY.test(value)) return "";
  const date = new Date(`${value}T00:00:00Z`);
  return Number.isNaN(date.getTime()) || date.toISOString().slice(0, 10) !== value ? "" : value;
}

const token = (value: string, max = 128) => (value.length <= max && TOKEN.test(value) ? value : "");

export function parseAuditFilters(params: Params): AuditFilters {
  const actorType = get(params, "actor_type");
  return {
    action: token(get(params, "action")),
    actorType: ACTOR_TYPES.includes(actorType as ActorTypeFilter) ? (actorType as ActorTypeFilter) : "",
    actorId: token(get(params, "actor_id")),
    entityType: token(get(params, "entity_type"), 64),
    entityId: token(get(params, "entity_id")),
    workflow: WORKFLOW.test(get(params, "workflow")) ? get(params, "workflow") : "",
    requestId: token(get(params, "request_id")),
    from: validDay(get(params, "from")),
    to: validDay(get(params, "to")),
    order: get(params, "order") === "asc" ? "asc" : "desc",
  };
}

export function parseAuditCursor(params: Params): string | null {
  const cursor = get(params, "cursor");
  return CURSOR.test(cursor) ? cursor : null;
}

/** URL query for the viewer page (and the export route), defaults left out. */
export function auditQuery(filters: AuditFilters, extra: Record<string, string> = {}): string {
  const params = new URLSearchParams();
  const pairs: [string, string][] = [
    ["action", filters.action],
    ["actor_type", filters.actorType],
    ["actor_id", filters.actorId],
    ["entity_type", filters.entityType],
    ["entity_id", filters.entityId],
    ["workflow", filters.workflow],
    ["request_id", filters.requestId],
    ["from", filters.from],
    ["to", filters.to],
    ["order", filters.order === "asc" ? "asc" : ""],
    ...Object.entries(extra),
  ];
  for (const [key, value] of pairs) if (value) params.set(key, value);
  const query = params.toString();
  return query ? `?${query}` : "";
}

function nextDay(day: string): string {
  const date = new Date(`${day}T00:00:00Z`);
  date.setUTCDate(date.getUTCDate() + 1);
  return date.toISOString();
}

/** Query parameters for `GET /v1/audit/events` and `/v1/audit/export`. */
export function auditApiQuery(filters: AuditFilters) {
  return {
    ...(filters.action ? { action: filters.action } : {}),
    ...(filters.actorType ? { actor_type: filters.actorType } : {}),
    ...(filters.actorId ? { actor_id: filters.actorId } : {}),
    ...(filters.entityType ? { entity_type: filters.entityType } : {}),
    ...(filters.entityId ? { entity_id: filters.entityId } : {}),
    ...(filters.workflow ? { workflow: filters.workflow } : {}),
    ...(filters.requestId ? { request_id: filters.requestId } : {}),
    ...(filters.from ? { from: `${filters.from}T00:00:00Z` } : {}),
    ...(filters.to ? { to: nextDay(filters.to) } : {}),
  };
}

export function hasAuditFilters(filters: AuditFilters): boolean {
  return Object.entries(filters).some(([key, value]) => key !== "order" && value !== "");
}
