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
export const eraseDataRequest = unavailable;
export const fileAppeal = unavailable;
export const indexPolicyDocument = unavailable;
export const inviteMember = unavailable;
export const loadApproval = unavailable;
export const loadDataRequest = unavailable;
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
export const resolveDataRequest = unavailable;
export const revokeApiKey = unavailable;
export const revokeInvitation = unavailable;
export const rotateWebhookSecret = unavailable;
export const savePreferences = unavailable;
export const searchEverything = unavailable;
export const setFlag = unavailable;
export const setRecruiters = unavailable;
export const setWebhookActive = unavailable;
export const switchOrganization = unavailable;
export const uploadPolicyDocument = unavailable;
export const verifyChain = unavailable;
// Pairly Hire pipeline and messaging (lib/actions/hire.ts, lib/actions/candidate-prefs.ts).
export const loadBoard = unavailable;
export const loadProfile = unavailable;
export const moveCandidate = unavailable;
export const advanceCandidate = unavailable;
export const undoMoves = unavailable;
export const bulkPreview = unavailable;
export const bulkMove = unavailable;
export const addNote = unavailable;
export const setTags = unavailable;
export const loadTeam = unavailable;
export const loadTemplates = unavailable;
export const createMessage = unavailable;
export const loadMessage = unavailable;
export const editMessage = unavailable;
export const aiDraftMessage = unavailable;
export const sendMessage = unavailable;
export const submitMessage = unavailable;
export const cancelMessage = unavailable;
export const bulkMessagePreview = unavailable;
export const bulkMessageSend = unavailable;
export const rediscover = unavailable;
export const referralLink = unavailable;
export const myReferrals = unavailable;
export const readCandidatePreferences = unavailable;
export const saveCandidatePreferences = unavailable;
// Pairly Hire screening mode (lib/actions/screening.ts).
export const loadQueue = unavailable;
export const loadCandidate = unavailable;
export const overrideScores = unavailable;
export const setBlindMode = unavailable;
// Pairly Hire candidate portal (lib/actions/portal.ts).
export const requestPortalLink = unavailable;
export const openPortalSession = unavailable;
export const signOutOfPortal = unavailable;
export const loadPortalApplications = unavailable;
export const loadPortalApplication = unavailable;
export const withdrawApplication = unavailable;
export const replyToCompany = unavailable;
export const loadPrivacy = unavailable;
export const requestDeletion = unavailable;
export const requestHumanReview = unavailable;
// Pairly Hire scheduling (lib/actions/scheduling.ts, lib/actions/candidate-schedule.ts).
export const loadScheduling = unavailable;
export const findSlots = unavailable;
export const checkSlot = unavailable;
export const createInterview = unavailable;
export const updateInterview = unavailable;
export const cancelInterview = unavailable;
export const resendLink = unavailable;
export const recordOutcome = unavailable;
export const jobInterviews = unavailable;
export const loadIntegrations = unavailable;
export const startGoogle = unavailable;
export const finishGoogle = unavailable;
export const disconnectGoogle = unavailable;
export const myAvailability = unavailable;
export const saveMyAvailability = unavailable;
export const readSchedule = unavailable;
export const bookSchedule = unavailable;
export const cancelSchedule = unavailable;
export const draftJobDescription = unavailable;
export const checkJobText = unavailable;
export const createJob = unavailable;
export const updateJob = unavailable;
export const draftJobRubric = unavailable;
export const saveJobRubric = unavailable;
export const lockJobRubric = unavailable;
export const publishJob = unavailable;
export const createRequisition = unavailable;
export const cancelRequisition = unavailable;
export const openJobFromRequisition = unavailable;
export const saveInterviewPlan = unavailable;
export const decideDebrief = unavailable;
export const openScorecard = unavailable;
export const snoozeFromLink = unavailable;
export const loadScorecard = unavailable;
export const saveScorecard = unavailable;
export const snoozeScorecard = unavailable;
