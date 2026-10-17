// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
"use server";

/**
 * Server actions for the admin settings pages: feature flags, API keys, webhooks, policy
 * documents and rule sets. Every one is checked by the API (`core.flags.manage`,
 * `core.api_keys.manage`, `core.webhooks.manage`, `core.policy.manage`, all step-up
 * permissions). Secrets the API returns once (an API key, a webhook signing secret) go
 * straight to the person who created them and are never stored here. Policy document
 * uploads go through the `/api/policies/documents` route handler instead (large body).
 */
import { z } from "zod";

import type { ActionResult } from "../action-result";
import { callApi, toAction } from "../api/call";
import type {
  ApiKey,
  ApiKeyCreated,
  DryRun,
  Flag,
  PolicyDocument,
  RetentionHold,
  RetentionScheduleItem,
  RetentionSettings,
  RuleSetVersion,
  WebhookDelivery,
  WebhookEndpoint,
  WebhookEndpointWithSecret,
} from "../api/types";

const invalid = { ok: false as const, code: "invalid_input" };
const flagKey = z.string().regex(/^[a-z][a-z0-9_.-]{0,127}$/);

// ------------------------------------------------------------------ flags

export async function setFlag(key: string, enabled: boolean): Promise<ActionResult<{ flag: Flag }>> {
  if (!flagKey.safeParse(key).success || typeof enabled !== "boolean") return invalid;
  const result = await callApi((api) => api.PUT("/v1/flags/{key}", { params: { path: { key } }, body: { enabled } }));
  return toAction(result, (flag) => ({ flag }));
}

export async function resetFlag(key: string): Promise<ActionResult<{ flag: Flag }>> {
  if (!flagKey.safeParse(key).success) return invalid;
  const result = await callApi((api) => api.DELETE("/v1/flags/{key}", { params: { path: { key } } }));
  return toAction(result, (flag) => ({ flag }));
}

// ------------------------------------------------------------------ API keys

const permission = z.string().regex(/^[a-z][a-z0-9_]{0,31}\.[a-z][a-z0-9_]{0,63}\.[a-z]+$/);
const apiKeySchema = z.object({
  name: z.string().trim().min(1).max(100),
  scopes: z.array(permission).min(1).max(100),
  mode: z.enum(["live", "test"]),
  expiresInDays: z.number().int().min(1).max(3650),
});

export async function createApiKey(input: z.infer<typeof apiKeySchema>): Promise<ActionResult<{ key: ApiKeyCreated }>> {
  const parsed = apiKeySchema.safeParse(input);
  if (!parsed.success) return invalid;
  const { name, scopes, mode, expiresInDays } = parsed.data;
  const result = await callApi((api) =>
    api.POST("/v1/api-keys", { body: { name, scopes, mode, expires_in_days: expiresInDays } }),
  );
  return toAction(result, (key) => ({ key }));
}

export async function revokeApiKey(id: string): Promise<ActionResult<{ key: ApiKey }>> {
  if (!z.uuid().safeParse(id).success) return invalid;
  const result = await callApi((api) =>
    api.POST("/v1/api-keys/{key_id}/revoke", { params: { path: { key_id: id } } }),
  );
  return toAction(result, (key) => ({ key }));
}

// ------------------------------------------------------------------ webhooks

const eventType = z.string().min(3).max(100).regex(/^[a-z*][a-z0-9_.*]*$/);
const endpointSchema = z.object({
  url: z.url({ protocol: /^https?$/ }).max(2048),
  description: z.string().trim().max(200).nullable(),
  eventTypes: z.array(eventType).min(1).max(100),
});

export async function createWebhook(
  input: z.infer<typeof endpointSchema>,
): Promise<ActionResult<{ endpoint: WebhookEndpointWithSecret }>> {
  const parsed = endpointSchema.safeParse(input);
  if (!parsed.success) return invalid;
  const result = await callApi((api) =>
    api.POST("/v1/webhooks/endpoints", {
      body: {
        url: parsed.data.url,
        description: parsed.data.description || null,
        event_types: parsed.data.eventTypes,
      },
    }),
  );
  return toAction(result, (endpoint) => ({ endpoint }));
}

export async function setWebhookActive(id: string, active: boolean): Promise<ActionResult<{ endpoint: WebhookEndpoint }>> {
  if (!z.uuid().safeParse(id).success || typeof active !== "boolean") return invalid;
  const result = await callApi((api) =>
    api.PATCH("/v1/webhooks/endpoints/{endpoint_id}", { params: { path: { endpoint_id: id } }, body: { active } }),
  );
  return toAction(result, (endpoint) => ({ endpoint }));
}

export async function rotateWebhookSecret(id: string): Promise<ActionResult<{ endpoint: WebhookEndpointWithSecret }>> {
  if (!z.uuid().safeParse(id).success) return invalid;
  const result = await callApi((api) =>
    api.POST("/v1/webhooks/endpoints/{endpoint_id}/rotate-secret", { params: { path: { endpoint_id: id } } }),
  );
  return toAction(result, (endpoint) => ({ endpoint }));
}

export async function loadDeliveries(id: string): Promise<ActionResult<{ deliveries: WebhookDelivery[] }>> {
  if (!z.uuid().safeParse(id).success) return invalid;
  const result = await callApi((api) =>
    api.GET("/v1/webhooks/endpoints/{endpoint_id}/deliveries", {
      params: { path: { endpoint_id: id }, query: { limit: 25 } },
    }),
  );
  return toAction(result, (deliveries) => ({ deliveries }));
}

// ------------------------------------------------------------------ policies

const overrideSchema = z.object({ id: z.uuid(), reason: z.string().trim().min(10).max(2000) });

/** Override the guardrails of a document that needs review (a second admin may be needed). */
export async function overridePolicyDocument(
  input: z.infer<typeof overrideSchema>,
): Promise<ActionResult<{ status: string; secondAdminRequired: boolean; policy: PolicyDocument }>> {
  const parsed = overrideSchema.safeParse(input);
  if (!parsed.success) return invalid;
  const result = await callApi((api) =>
    api.POST("/v1/policies/documents/{policy_document_id}/override", {
      params: { path: { policy_document_id: parsed.data.id } },
      body: { reason: parsed.data.reason },
    }),
  );
  return toAction(result, (data) => ({
    status: data.status,
    secondAdminRequired: data.second_admin_required,
    policy: data.policy_document,
  }));
}

export async function indexPolicyDocument(id: string): Promise<ActionResult<{ status: string }>> {
  if (!z.uuid().safeParse(id).success) return invalid;
  const result = await callApi((api) =>
    api.POST("/v1/policies/documents/{policy_document_id}/index", { params: { path: { policy_document_id: id } } }),
  );
  return toAction(result, (data) => ({ status: data.status }));
}

export async function archivePolicyDocument(id: string): Promise<ActionResult<{ policy: PolicyDocument }>> {
  if (!z.uuid().safeParse(id).success) return invalid;
  const result = await callApi((api) =>
    api.POST("/v1/policies/documents/{policy_document_id}/archive", { params: { path: { policy_document_id: id } } }),
  );
  return toAction(result, (policy) => ({ policy }));
}

const ruleSetName = z.string().regex(/^[a-z][a-z0-9_.]{0,127}$/);

export async function loadRuleSetVersions(name: string): Promise<ActionResult<{ versions: RuleSetVersion[] }>> {
  if (!ruleSetName.safeParse(name).success) return invalid;
  const result = await callApi((api) =>
    api.GET("/v1/policies/rule-sets/{name}/versions", { params: { path: { name } } }),
  );
  return toAction(result, (versions) => ({ versions }));
}

export async function loadRuleSetVersion(
  name: string,
  version: number,
): Promise<ActionResult<{ version: RuleSetVersion }>> {
  if (!ruleSetName.safeParse(name).success || !Number.isInteger(version) || version < 1) return invalid;
  const result = await callApi((api) =>
    api.GET("/v1/policies/rule-sets/{name}", { params: { path: { name }, query: { version } } }),
  );
  return toAction(result, (data) => ({ version: data }));
}

const dryRunSchema = z.object({
  name: ruleSetName,
  version: z.number().int().min(1).nullable(),
  facts: z.string().max(64 * 1024),
});

/** Evaluate sample facts (JSON) against a published version. Nothing is stored. */
export async function dryRunRuleSet(input: z.infer<typeof dryRunSchema>): Promise<ActionResult<{ result: DryRun }>> {
  const parsed = dryRunSchema.safeParse(input);
  if (!parsed.success) return invalid;
  let facts: unknown;
  try {
    facts = JSON.parse(parsed.data.facts);
  } catch {
    return { ok: false, code: "invalid_json" };
  }
  if (typeof facts !== "object" || facts === null || Array.isArray(facts)) return { ok: false, code: "invalid_json" };
  const result = await callApi((api) =>
    api.POST("/v1/policies/rule-sets/dry-run", {
      body: {
        name: parsed.data.name,
        ...(parsed.data.version ? { version: parsed.data.version } : {}),
        facts: facts as Record<string, never>,
      },
    }),
  );
  return toAction(result, (data) => ({ result: data }));
}

// ------------------------------------------------------------------ retention

const ruleKey = z.string().regex(/^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*){0,3}$/).max(100);
const ruleChange = z
  .object({
    rule: ruleKey,
    enabled: z.boolean().optional(),
    days: z.number().int().min(0).max(36500).optional(),
    period: z.number().int().min(0).max(36500).optional(),
  })
  .strict();

/** Switch a retention rule on or off, or change its period (`core.retention.manage`). */
export async function updateRetentionRule(
  change: z.infer<typeof ruleChange>,
): Promise<ActionResult<{ settings: RetentionSettings }>> {
  const parsed = ruleChange.safeParse(change);
  if (!parsed.success) return invalid;
  const { rule, enabled, days, period } = parsed.data;
  const body = { rule, enabled: enabled ?? null, days: days ?? null, period: period ?? null };
  const result = await callApi((api) => api.PUT("/v1/retention/settings", { body: { rules: [body] } }));
  return toAction(result, (settings) => ({ settings }));
}

/** The most subjects deleted a day (`core.retention.manage`). */
export async function setRetentionCap(cap: number): Promise<ActionResult<{ settings: RetentionSettings }>> {
  if (!z.number().int().min(1).max(10000).safeParse(cap).success) return invalid;
  const result = await callApi((api) => api.PUT("/v1/retention/settings", { body: { daily_cap: cap } }));
  return toAction(result, (settings) => ({ settings }));
}

const holdSchema = z.object({
  subjectType: z.string().regex(/^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$/).max(64),
  subjectId: z.uuid(),
  reason: z.string().trim().min(1).max(2000),
});

/** A legal hold on a candidate or a vendor: nothing about them is deleted while it stands. */
export async function placeRetentionHold(input: z.infer<typeof holdSchema>): Promise<ActionResult<{ hold: RetentionHold }>> {
  const parsed = holdSchema.safeParse(input);
  if (!parsed.success) return invalid;
  const { subjectType, subjectId, reason } = parsed.data;
  const result = await callApi((api) =>
    api.POST("/v1/retention/holds", { body: { subject_type: subjectType, subject_id: subjectId, reason } }),
  );
  return toAction(result, (hold) => ({ hold }));
}

export async function releaseRetentionHold(id: string): Promise<ActionResult<{ hold: RetentionHold }>> {
  if (!z.uuid().safeParse(id).success) return invalid;
  const result = await callApi((api) =>
    api.POST("/v1/retention/holds/{hold_id}/release", { params: { path: { hold_id: id } } }),
  );
  return toAction(result, (hold) => ({ hold }));
}

/**
 * Pause a retention rule (nothing of it is deleted until resumed), or resume it: every
 * listed subject then gets a new seven-day preview, so the list comes back with it.
 */
export async function setRetentionPaused(
  rule: string,
  paused: boolean,
): Promise<ActionResult<{ settings: RetentionSettings; schedule: RetentionScheduleItem[] | null }>> {
  if (!ruleKey.safeParse(rule).success || typeof paused !== "boolean") return invalid;
  const params = { path: { rule } };
  const result = paused
    ? await callApi((api) => api.POST("/v1/retention/rules/{rule}/pause", { params }))
    : await callApi((api) => api.POST("/v1/retention/rules/{rule}/resume", { params }));
  if (!result.ok) return { ok: false, code: result.code };
  const schedule = await callApi((api) => api.GET("/v1/retention/schedule"));
  return { ok: true, settings: result.data, schedule: schedule.ok ? schedule.data : null };
}
