# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Workflow intake for documents submitted through the batch API.

A workflow plugin may declare an optional ``document_intake`` attribute (a
``DocumentIntake``). The batch API then, for every new or duplicate document:

1. authorizes ``permission`` once for the batch (instead of the generic document upload
   permission), so a key that may submit invoices may batch-submit invoices;
2. calls ``handle`` in the request's transaction, which turns the document into the
   workflow's own entity (Pairly Pay: an invoice) and says which;
3. after the commit, runs ``after_commit`` (Pay: starts the invoice run).

``policy``, when set, is the workflow's intake policy (allowed types, size) instead of
the configured default. A workflow without an intake still gets its documents ingested,
scanned and parsed; nothing more starts.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from pairly_core.ingestion.models import Document
    from pairly_core.ingestion.settings import IngestionSettings, IntakePolicy
    from pairly_core.rbac import Actor
    from pairly_core.workflow.runtime import WorkflowRuntime


class IntakeRegistryError(ValueError):
    """A plugin declared an invalid ``document_intake`` (a programming error)."""


@dataclass(frozen=True, slots=True)
class IntakeResult:
    entity_type: str
    entity_id: str
    created: bool
    # Run after the request committed (never inside its transaction).
    after_commit: Callable[[WorkflowRuntime], Awaitable[None]] | None = None


IntakeHandler = Callable[["AsyncSession", "Actor", "Document"], Awaitable[IntakeResult]]


@dataclass(frozen=True, slots=True)
class DocumentIntake:
    permission: str
    handle: IntakeHandler
    policy: Callable[[IngestionSettings], IntakePolicy] | None = None


def build_intake_registry(plugins: Iterable[Any]) -> dict[str, DocumentIntake]:
    """Each plugin's optional ``document_intake``, by workflow id."""
    registry: dict[str, DocumentIntake] = {}
    for plugin in plugins:
        intake = getattr(plugin, "document_intake", None)
        if intake is None:
            continue
        if not isinstance(intake, DocumentIntake):
            raise IntakeRegistryError(
                f"plugin {plugin.id!r}: document_intake must be a DocumentIntake"
            )
        if not intake.permission.startswith(f"{plugin.id}."):
            raise IntakeRegistryError(
                f"plugin {plugin.id!r}: the intake permission must be one of its own"
            )
        registry[str(plugin.id)] = intake
    return registry
