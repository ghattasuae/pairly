# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""What an approver is shown of an approval's payload and context.

An approval binds the hash of its exact payload (``approvals.payload_hash``); the final
action re-checks that hash, so what is executed is always the real payload. A workflow
may still need to show an approver less than the real payload: Pairly Hire hides a
candidate's name, email and phone from the people who approve a rejection while blind
mode is on, although the email that is sent carries them.

A workflow registers a view filter per approval type (``register_view_filter``). The
approval detail route calls ``approval_view``: without a filter the approver sees the
stored payload and context as they are. With one, the filter returns the payload and
context to show and says what it masked (``masked``) with one plain sentence
(``note``). The filter only changes what is displayed; the stored payload, its hash and
the decision are untouched.

Fail closed: a filter that raises shows nothing of the payload or context (the approver
is told why), never the unfiltered content. The filter runs in a savepoint, so a failed
query cannot break the caller's transaction.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Final

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from pairly_core.approvals.models import Approval

log = structlog.get_logger(__name__)

HIDDEN_NOTE: Final = (
    "This content cannot be shown right now. Nothing changes until someone can review it."
)


@dataclass(frozen=True, slots=True)
class ApprovalView:
    payload: dict[str, Any]
    context: dict[str, Any]
    # What was hidden from the approver (for example ``name``, ``email``, ``phone``).
    masked: tuple[str, ...] = field(default=())
    note: str | None = None


ViewFilter = Callable[[AsyncSession, Approval], Awaitable[ApprovalView | None]]

_FILTERS: dict[str, ViewFilter] = {}


def register_view_filter(approval_type: str, view: ViewFilter) -> None:
    """Show approvals of ``approval_type`` through ``view`` (one filter per type)."""
    _FILTERS[approval_type] = view


def unregister_view_filter(approval_type: str) -> None:
    _FILTERS.pop(approval_type, None)


async def approval_view(session: AsyncSession, approval: Approval) -> ApprovalView:
    """The payload and context the approver sees (see the module docstring)."""
    raw = ApprovalView(dict(approval.payload or {}), dict(approval.context or {}))
    view = _FILTERS.get(approval.approval_type)
    if view is None:
        return raw
    try:
        async with session.begin_nested():
            shown = await view(session, approval)
    except Exception as exc:
        log.warning(
            "approval_view_failed",
            approval_id=str(approval.id),
            approval_type=approval.approval_type,
            error=type(exc).__name__,
        )
        return ApprovalView({}, {}, masked=("payload",), note=HIDDEN_NOTE)
    return shown if shown is not None else raw
