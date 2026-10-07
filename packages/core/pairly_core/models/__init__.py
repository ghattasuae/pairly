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
from pairly_core.extraction.models import Extraction, ExtractionCorrection, ExtractionField
from pairly_core.flags.models import FeatureFlag
from pairly_core.idempotency import IdempotencyRecord
from pairly_core.ingestion.models import Document, InboundAddress, InboundEmail
from pairly_core.jobs.models import JobRunRow
from pairly_core.models.audit import AuditEvent
from pairly_core.models.llm import LLMBudgetAlert, LLMBudgetReservation, ModelCall, OrgLLMBudget
from pairly_core.models.policy import PolicyVersion
from pairly_core.models.tenancy import Invitation, Membership, Organisation, Role, User
from pairly_core.policy.models import PolicyDocument, RuleResult
from pairly_core.retention.models import RetentionReport
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
    "Approval",
    "ApprovalDecision",
    "ApprovalDelegation",
    "ApprovalLink",
    "ApprovalStep",
    "AuditEvent",
    "Document",
    "Extraction",
    "ExtractionCorrection",
    "ExtractionField",
    "FeatureFlag",
    "IdempotencyRecord",
    "InboundAddress",
    "InboundEmail",
    "InboundEvent",
    "Invitation",
    "JobRunRow",
    "LLMBudgetAlert",
    "LLMBudgetReservation",
    "Membership",
    "ModelCall",
    "OrgLLMBudget",
    "Organisation",
    "PolicyDocument",
    "PolicyVersion",
    "RetentionReport",
    "RiskAssessment",
    "RiskSignalRow",
    "Role",
    "RuleResult",
    "SearchChunk",
    "User",
    "WebhookDelivery",
    "WebhookEndpoint",
    "WebhookOutbox",
    "WorkflowRun",
]
