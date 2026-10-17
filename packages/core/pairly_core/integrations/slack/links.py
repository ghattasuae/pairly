# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Lookups shared by notifications and the Slack service (tenant-bound sessions only)."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from pairly_core.crypto.types import unseal
from pairly_core.integrations.models import Integration, SlackUserLink

SLACK = "slack"


async def active_slack(session: AsyncSession) -> Integration | None:
    """The organisation's active Slack integration, if any."""
    return await session.scalar(
        select(Integration).where(Integration.provider == SLACK, Integration.status == "active")
    )


def bot_token(integration: Integration) -> str:
    value = unseal(integration, "credential")
    if not isinstance(value, str):  # pragma: no cover - column type
        raise TypeError("credential is not a string")
    return value


async def slack_link_for_user(
    session: AsyncSession, user_id: uuid.UUID, team_id: str
) -> SlackUserLink | None:
    return await session.scalar(
        select(SlackUserLink).where(
            SlackUserLink.user_id == user_id, SlackUserLink.team_id == team_id
        )
    )


async def user_for_slack(
    session: AsyncSession, team_id: str, slack_user_id: str
) -> SlackUserLink | None:
    return await session.scalar(
        select(SlackUserLink).where(
            SlackUserLink.team_id == team_id, SlackUserLink.slack_user_id == slack_user_id
        )
    )
