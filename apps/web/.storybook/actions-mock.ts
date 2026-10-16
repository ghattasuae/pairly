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
export const updateRetentionRule = unavailable;
export const setRetentionCap = unavailable;
export const placeRetentionHold = unavailable;
export const releaseRetentionHold = unavailable;
export const setRetentionPaused = unavailable;
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
export const startConnect = unavailable;
export const finishConnect = unavailable;
export const disconnectCalendar = unavailable;
export const loadOrgIntegrations = unavailable;
export const saveZoom = unavailable;
export const removeZoom = unavailable;
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
// Reference checks (lib/actions/references.ts, lib/actions/referee.ts).
export const askReferee = unavailable;
export const resendReference = unavailable;
export const cancelReference = unavailable;
export const openReference = unavailable;
export const loadReference = unavailable;
export const saveReference = unavailable;
// Offers (lib/actions/offers.ts, lib/actions/portal-offer.ts).
export const createOffer = unavailable;
export const offerAction = unavailable;
export const answerOffer = unavailable;
// Pre-employment checklist (lib/actions/checklist.ts).
export const requestChecklist = unavailable;
export const reviewChecklistItem = unavailable;
export const saveChecklistConfig = unavailable;
// Candidate experience survey (lib/actions/survey.ts).
export const viewSurvey = unavailable;
export const answerSurvey = unavailable;
// Fairness runs and calibration mode (lib/actions/fairness.ts, lib/actions/calibration.ts).
export const startFairnessRun = unavailable;
export const loadCalibrationItem = unavailable;
export const saveCalibrationLabels = unavailable;
export const suggestClarification = unavailable;
// Pay vendors, bank-detail changes and onboarding (lib/actions/pay.ts,
// lib/actions/vendor-onboarding.ts).
export const checkVendor = unavailable;
export const createVendor = unavailable;
export const requestBankChange = unavailable;
export const recordCall = unavailable;
export const startApproval = unavailable;
export const cancelBankChange = unavailable;
export const vendorAction = unavailable;
export const createInvite = unavailable;
export const revokeInvite = unavailable;
export const openVendorLink = unavailable;
export const loadVendorForm = unavailable;
export const submitVendorForm = unavailable;
// Pay invoices (lib/actions/pay.ts).
export const correctInvoiceField = unavailable;
export const invoiceAction = unavailable;
export const codeInvoice = unavailable;
export const previewRun = unavailable;
export const proposeRun = unavailable;
export const runAction = unavailable;
export const importPurchaseCsv = unavailable;
export const savePaySettings = unavailable;
export const codeInvoiceLines = unavailable;
export const askModelCoding = unavailable;
export const draftExceptionMessage = unavailable;
export const sendExceptionMessage = unavailable;
export const endPurchaseOrder = unavailable;
export const givePortalAccess = unavailable;
export const revokePortalAccess = unavailable;
export const replyInPortal = unavailable;
export const applyCreditNote = unavailable;
export const confirmSlackConnect = unavailable;
export const requestVendorPortalLink = unavailable;
export const openVendorPortalSession = unavailable;
export const signOutOfVendorPortal = unavailable;
export const loadVendorInvoices = unavailable;
export const writeToAccountsPayable = unavailable;
export const askForHumanReview = unavailable;
export const requestBankDetailChange = unavailable;
export const updatePaymentTerms = unavailable;
export const importStatement = unavailable;
export const matchStatementsNow = unavailable;
