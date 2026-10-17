// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology

/**
 * Types generated from the Pairly OpenAPI schema (openapi-typescript), plus a few named
 * aliases the web app and SDK use. Regenerate with `pnpm gen:api`.
 */
import type { components, paths } from "./generated/api.js";

export type { components, operations, paths } from "./generated/api.js";

/** Workflow ids known to the platform. Mirrors the `pairly.workflows` plugin ids. */
export type WorkflowId = "hire" | "pay";

export const API_VERSION = "v1" as const;

/** `GET /v1/me`: the user, the selected organisation, roles and effective permissions. */
export type Me = components["schemas"]["MeOut"];

/** An organisation the user belongs to (`GET /v1/me/orgs`). */
export type OrgSummary = components["schemas"]["OrgOut"];

/** `GET /v1/members` item. */
export type Member = components["schemas"]["MemberOut"];

/** An invitation as listed by `GET /v1/invitations`. */
export type Invitation = components["schemas"]["InvitationOut"];

/** `POST /v1/invitations/preview`: what an invitation token invites to. */
export type InvitationPreview = components["schemas"]["InvitationPreviewOut"];

/** Every error body (RFC 9457 problem details). */
export interface Problem {
  type: string;
  title: string;
  status: number;
  detail: string;
  code: string;
  request_id?: string;
}

/** The header that selects the organisation (tenant) for a request. */
export const ORG_HEADER = "X-Pairly-Org" as const;

export type ApiPaths = paths;
