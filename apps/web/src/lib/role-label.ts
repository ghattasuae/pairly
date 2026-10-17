// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology

/** A translator for one namespace, loosely typed so role keys from the API can be looked up. */
export interface Lookup {
  (key: string): string;
  has: (key: string) => boolean;
}

/**
 * "Approver · Pay": the localized role name and, for a workflow-scoped role, the workflow.
 * Keys the message files do not know (custom roles) are shown as they are.
 */
export function formatRole(roles: Lookup, workflows: Lookup, role: string, workflow: string | null): string {
  const name = roles.has(role) ? roles(role) : role;
  if (!workflow) return name;
  return `${name} · ${workflows.has(workflow) ? workflows(workflow) : workflow}`;
}
