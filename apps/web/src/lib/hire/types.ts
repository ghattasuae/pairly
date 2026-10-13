// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology

/** Hire pipeline and messaging shapes, generated from the OpenAPI schema (`pnpm gen:api`). */
import type { components } from "@pairly/shared-ts";

type S = components["schemas"];

export type HireBoard = S["HireBoardOut"];
export type HireCard = S["HireCardOut"];
export type HireStage = S["HireStageOut"];
export type HireProfile = S["HireProfileOut"];
export type HireEvent = S["HireEventOut"];
export type HireMessageSummary = S["HireMessageSummaryOut"];
export type HireMove = S["HireMoveOut"];
export type HireUndo = S["HireUndoOut"];
export type HireBulkSummary = S["HireBulkSummaryOut"];
export type HireBulkResult = S["HireBulkOut"];
export type HireTeamMember = S["HireTeamMemberOut"];
export type HireTemplate = S["HireTemplateOut"];
export type HireTemplateField = S["HireTemplateFieldOut"];
export type HireMessage = S["HireMessageOut"];
export type HireAiDraft = S["HireAiDraftOut"];
export type HireBulkMessageSummary = S["HireBulkMessageSummaryOut"];
export type HireWaitingList = S["HireWaitingList"];
export type HireWaiting = S["HireWaitingOut"];
export type HireRediscoveryHit = S["HireRediscoveryHit"];
export type HireReferral = S["HireReferralOut"];
export type HireJob = S["HireJobOut"];
export type HireDataRequest = S["HireDataRequestOut"];
export type HirePrefs = S["HirePrefsOut"];
export type HireJobInput = S["HireJobIn"];
export type HireJobDraft = S["HireJobDraftOut"];
export type HireTextChecks = S["HireTextChecks"];
export type HireFinding = S["HireFindingOut"];
export type HireRubricState = S["HireRubricOut"];
export type HireRubricLock = S["HireRubricLockOut"];
export type HireRequisition = S["HireRequisitionOut"];
export type HireRequisitionInput = S["HireRequisitionIn"];
export type HireInterviewPlanView = S["HireInterviewPlanOut"];
export type HireInterviewPlan = S["InterviewPlan"];
export type HirePlanStage = S["PlanStage"];
export type HirePlanQuestion = S["PlanQuestion"];
export type HireKit = S["HireKitOut"];
export type HireKitItem = S["HireKitItemOut"];
export type HireScorecardPage = S["HireScorecardPageOut"];
export type HireScorecard = S["HireScorecardOut"];
export type HireOtherScorecard = S["HireOtherScorecardOut"];
export type HireDebrief = S["HireDebriefOut"];
export type HireReferences = S["HireReferencesOut"];
export type HireReference = S["HireReferenceOut"];
export type HireRefereePage = S["HireRefereePageOut"];
export type HireOffer = S["HireOfferOut"];
export type HireOfferList = S["HireOfferListOut"];
export type HireOfferTemplate = S["HireOfferTemplateOut"];
export type HirePortalOffer = S["HirePortalOfferOut"];
export type HireChecklist = S["HireChecklistOut"];
export type HireChecklistItem = S["HireChecklistItemOut"];
export type HireChecklistConfig = S["HireChecklistConfigOut"];
export type HireSurveyForm = S["HireSurveyFormOut"];
