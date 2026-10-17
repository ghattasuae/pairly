// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology

/** Named API shapes the web app uses, generated from the OpenAPI schema (`pnpm gen:api`). */
import type { components } from "@pairly/shared-ts";

type S = components["schemas"];

export type ApprovalSummary = S["ApprovalOut"];
export type ApprovalDetail = S["ApprovalDetailOut"];
export type ApprovalStep = S["StepOut"];
export type DecisionRow = S["DecisionRowOut"];
export type DecisionChoice = S["Decision"];
export type DecisionResult = S["DecisionOut"];
export type ApprovalLinkView = S["LinkOut"];
export type SlaState = NonNullable<ApprovalSummary["sla_state"]>;

export type DocumentInfo = S["DocumentOut"];
export type ContentUrl = S["ContentUrlOut"];
export type Extraction = S["ExtractionOut"];
export type ExtractedField = S["FieldOut"];

export type Replay = S["ReplayOut"];
export type Appeal = S["AppealOut"];
export type AppealOutcome = S["AppealOutcome"];
export type RequesterType = S["RequesterType"];

export type AuditEvent = S["AuditEventOut"];
export type AuditPage = S["AuditPage"];
export type ChainCheck = S["VerifyOut"];
export type ChainJob = S["VerifyJobOut"];
export type Anchor = S["AnchorOut"];

export type Notification = S["NotificationOut"];
export type NotificationPage = S["NotificationPage"];
export type Preferences = S["PreferencesOut"];
export type PreferencesInput = S["PreferencesIn"];

export type Flag = S["FlagOut"];
export type RetentionSettings = S["RetentionSettingsOut"];
export type RetentionRuleSetting = S["RetentionRuleSettingOut"];
export type RetentionScheduleItem = S["RetentionScheduleItemOut"];
export type RetentionHold = S["RetentionHoldOut"];
export type RetentionReport = S["ReportOut"];
export type ApiKey = S["ApiKeyOut"];
export type ApiKeyCreated = S["ApiKeyCreatedOut"];
export type WebhookEndpoint = S["EndpointOut"];
export type WebhookEndpointWithSecret = S["EndpointSecretOut"];
export type WebhookEventType = S["EventTypeOut"];
export type WebhookDelivery = S["DeliveryOut"];

export type PolicyDocument = S["PolicyDocumentOut"];
export type PolicyWarning = S["PolicyWarningOut"];
export type RuleSetVersion = S["PolicyVersionOut"];
export type DryRun = S["DryRunOut"];
export type RuleResult = S["RuleResultOut"];

export type SearchHit = S["HitOut"];
export type SearchKind = S["KindOut"];

/** A box on a page in the document's own units (PDF points, image pixels), top-left origin. */
export interface BBox {
  x0: number;
  top: number;
  x1: number;
  bottom: number;
}
