// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
"use server";

/**
 * Server actions for the admin settings pages: feature flags, API keys, webhooks, policy
 * documents and rule sets. Every one is checked by the API (`core.flags.manage`,
 * `core.api_keys.manage`, `core.webhooks.manage`, `core.policy.manage`, all step-up
 * permissions). Secrets the API returns once (an API key, a webhook signing secret) go
 * straight to the person who created them and are never stored here.
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

const policyName = z.string().regex(/^[a-z][a-z0-9_.-]{0,127}$/);
const MAX_UPLOAD_BYTES = 25 * 1024 * 1024;

/** Upload a policy document as the next version of `name` (multipart, through ingestion). */
export async function uploadPolicyDocument(form: FormData): Promise<ActionResult<{ policy: PolicyDocument }>> {
  const name = form.get("name");
  const title = form.get("title");
  const file = form.get("file");
  if (
    !policyName.safeParse(name).success ||
    typeof title !== "string" ||
    title.trim().length < 1 ||
    title.length > 200 ||
    !(file instanceof File) ||
    file.size === 0 ||
    file.size > MAX_UPLOAD_BYTES
  ) {
    return invalid;
  }
  const body = new FormData();
  body.set("file", file, file.name.slice(0, 200) || "policy");
  const result = await callApi((api) =>
    api.POST("/v1/policies/documents", {
      params: { query: { name: name as string, title: title.trim() } },
      body: body as never,
      bodySerializer: (value: unknown) => value as FormData,
    }),
  );
  return toAction(result, (data) => ({ policy: data.policy_document }));
}

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
