# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Python client for the Pairly API: human-approved AI for hiring and accounts payable.

    from pairly import Pairly

    pairly = Pairly(api_key="pk_test_...")            # or PAIRLY_API_KEY
    job = pairly.documents.submit("invoice.pdf", workflow="pay")
    waiting = pairly.approvals.list_mine()

``AsyncPairly`` has the same surface with ``await``. Webhook helpers are in
``pairly.webhooks`` (``verify_signature``, ``construct_event``, typed events). People
decide in Pairly: API keys read, propose and upload, and never approve.
"""

from pairly import webhooks
from pairly._http import RetryPolicy
from pairly._version import __version__
from pairly.client import AsyncPairly, BatchTimeoutError, Pairly
from pairly.errors import (
    AuthenticationError,
    BadRequestError,
    ConflictError,
    NotFoundError,
    PairlyAPIError,
    PairlyConfigError,
    PairlyConnectionError,
    PairlyError,
    PermissionDeniedError,
    RateLimitError,
    ServerError,
)
from pairly.types import Approval, ApprovalPage, Batch, BatchItem, Proposal, Submission
from pairly.webhooks import WebhookEvent, WebhookSignatureError, construct_event, verify_signature

__all__ = [
    "Approval",
    "ApprovalPage",
    "AsyncPairly",
    "AuthenticationError",
    "BadRequestError",
    "Batch",
    "BatchItem",
    "BatchTimeoutError",
    "ConflictError",
    "NotFoundError",
    "Pairly",
    "PairlyAPIError",
    "PairlyConfigError",
    "PairlyConnectionError",
    "PairlyError",
    "PermissionDeniedError",
    "Proposal",
    "RateLimitError",
    "RetryPolicy",
    "ServerError",
    "Submission",
    "WebhookEvent",
    "WebhookSignatureError",
    "__version__",
    "construct_event",
    "verify_signature",
    "webhooks",
]
