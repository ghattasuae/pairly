# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Core engine ORM models. Importing this package registers them on ``Base.metadata``."""

from pairly_core.api_keys.models import ApiKey
from pairly_core.approvals.models import (
    Approval,
    ApprovalDecision,
    ApprovalDelegation,
    ApprovalLink,
    ApprovalStep,
)
from pairly_core.audit_anchor.models import AuditDenialLimit, AuditVerification
from pairly_core.batches.models import DocumentBatch, DocumentBatchItem
from pairly_core.decisions.models import Appeal, ComplianceExport, DecisionRecord, DecisionReplay
from pairly_core.extraction.models import Extraction, ExtractionCorrection, ExtractionField
from pairly_core.flags.models import FeatureFlag
from pairly_core.guardrails.redaction_counts import PiiRedactionCount
from pairly_core.idempotency import IdempotencyRecord
from pairly_core.ingestion.models import Document, InboundAddress, InboundEmail
from pairly_core.integrations.models import (
    Integration,
    SlackInteraction,
    SlackLinkRequest,
    SlackUserLink,
)
from pairly_core.jobs.models import JobRunRow
from pairly_core.models.audit import AuditEvent
from pairly_core.models.llm import LLMBudgetAlert, LLMBudgetReservation, ModelCall, OrgLLMBudget
from pairly_core.models.policy import PolicyVersion
from pairly_core.models.portal import PortalGuardRule
from pairly_core.models.tenancy import Invitation, Membership, Organisation, Role, User
from pairly_core.notifications.models import Notification, NotificationPreference, Template
from pairly_core.policy.models import PolicyDocument, RuleResult
from pairly_core.proposals.models import Proposal
from pairly_core.retention.models import (
    RetentionHold,
    RetentionOrgSetting,
    RetentionReport,
    RetentionRuleControl,
    RetentionRuleSetting,
    RetentionScheduleItem,
)
from pairly_core.retrieval.models import SearchChunk
from pairly_core.risk.models import RiskAssessment, RiskSignalRow
from pairly_core.webhooks.models import (
    InboundEvent,
    WebhookDelivery,
    WebhookEndpoint,
    WebhookOutbox,
)
from pairly_core.workflow.models import WorkflowRun

__all__ = [
    "ApiKey",
    "Appeal",
    "Approval",
    "ApprovalDecision",
    "ApprovalDelegation",
    "ApprovalLink",
    "ApprovalStep",
    "AuditDenialLimit",
    "AuditEvent",
    "AuditVerification",
    "ComplianceExport",
    "DecisionRecord",
    "DecisionReplay",
    "Document",
    "DocumentBatch",
    "DocumentBatchItem",
    "Extraction",
    "ExtractionCorrection",
    "ExtractionField",
    "FeatureFlag",
    "IdempotencyRecord",
    "InboundAddress",
    "InboundEmail",
    "InboundEvent",
    "Integration",
    "Invitation",
    "JobRunRow",
    "LLMBudgetAlert",
    "LLMBudgetReservation",
    "Membership",
    "ModelCall",
    "Notification",
    "NotificationPreference",
    "OrgLLMBudget",
    "Organisation",
    "PiiRedactionCount",
    "PolicyDocument",
    "PolicyVersion",
    "PortalGuardRule",
    "Proposal",
    "RetentionHold",
    "RetentionOrgSetting",
    "RetentionReport",
    "RetentionRuleControl",
    "RetentionRuleSetting",
    "RetentionScheduleItem",
    "RiskAssessment",
    "RiskSignalRow",
    "Role",
    "RuleResult",
    "SearchChunk",
    "SlackInteraction",
    "SlackLinkRequest",
    "SlackUserLink",
    "Template",
    "User",
    "WebhookDelivery",
    "WebhookEndpoint",
    "WebhookOutbox",
    "WorkflowRun",
]
