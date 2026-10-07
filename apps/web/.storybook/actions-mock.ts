// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology

/**
 * Storybook stand-in for every server action in src/lib/actions (aliased in main.ts).
 * Server actions call the API with a server-minted token, which a static Storybook cannot
 * do: each one answers "unavailable", and stories pass their own fakes where it matters.
 * A test in src/lib checks that this file names every exported action.
 */
const unavailable = async (..._args: unknown[]): Promise<{ ok: false; code: string } & Record<string, never>> => ({
  ok: false,
  code: "unavailable",
});

export const acceptInvitation = unavailable;
export const archivePolicyDocument = unavailable;
export const chainJob = unavailable;
export const confirmApprovalLink = unavailable;
export const confirmUnsubscribe = unavailable;
export const createApiKey = unavailable;
export const createWebhook = unavailable;
export const decideApproval = unavailable;
export const describeApprovalLink = async () => ({ kind: "unavailable" as const });
export const describeInvitation = async () => ({ kind: "unavailable" as const });
export const describeUnsubscribe = unavailable;
export const documentSource = unavailable;
export const dryRunRuleSet = unavailable;
export const fileAppeal = unavailable;
export const indexPolicyDocument = unavailable;
export const inviteMember = unavailable;
export const loadApproval = unavailable;
export const loadDeliveries = unavailable;
export const loadInboxPage = unavailable;
export const loadNotifications = unavailable;
export const loadRuleSetVersion = unavailable;
export const loadRuleSetVersions = unavailable;
export const markAllNotificationsRead = unavailable;
export const markNotificationRead = unavailable;
export const overridePolicyDocument = unavailable;
export const prepareInviteSignIn = unavailable;
export const replayDecision = unavailable;
export const resetFlag = unavailable;
export const resolveAppeal = unavailable;
export const revokeApiKey = unavailable;
export const revokeInvitation = unavailable;
export const rotateWebhookSecret = unavailable;
export const savePreferences = unavailable;
export const searchEverything = unavailable;
export const setFlag = unavailable;
export const setWebhookActive = unavailable;
export const switchOrganization = unavailable;
export const uploadPolicyDocument = unavailable;
export const verifyChain = unavailable;
