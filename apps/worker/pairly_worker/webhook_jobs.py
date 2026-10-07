# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Outbound webhook delivery job (WP1.11).

``deliver_webhooks`` (cron, every minute): for every organisation with undispatched
outbox events or due deliveries (``webhook_due_org_ids()``, SECURITY DEFINER, ids only),
fan the events out to deliveries and send the due ones
(``pairly_core.webhooks.delivery.WebhookDispatcher``). One failing organisation never
stops the others. The dispatcher is built at worker startup (``setup_webhooks``).
"""

from __future__ import annotations

from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from pairly_core.flags.registry import WEBHOOKS_OUTBOUND
from pairly_core.flags.service import is_enabled
from pairly_core.webhooks.delivery import (
    EndpointDisabledHook,
    HttpxSender,
    WebhookDispatcher,
    due_org_ids,
)
from pairly_core.webhooks.settings import load_webhook_settings

log = structlog.get_logger("pairly_worker.webhook_jobs")

DISPATCHER = "webhook_dispatcher"


async def _outbound_enabled(session: AsyncSession) -> bool:
    return await is_enabled(session, WEBHOOKS_OUTBOUND)


def setup_webhooks(
    ctx: dict[str, Any], *, on_endpoint_disabled: EndpointDisabledHook | None = None
) -> WebhookDispatcher:
    """Build the dispatcher (fails closed on invalid webhook settings)."""
    settings = load_webhook_settings()
    sender = HttpxSender(settings)
    # Organisations can switch delivery off with the webhooks.outbound flag.
    kwargs: dict[str, Any] = {"enabled_for": _outbound_enabled}
    if on_endpoint_disabled is not None:
        kwargs["on_endpoint_disabled"] = on_endpoint_disabled
    dispatcher = WebhookDispatcher(ctx["sessionmaker"], sender, settings, **kwargs)
    ctx[DISPATCHER] = dispatcher
    ctx["webhook_sender"] = sender
    return dispatcher


async def deliver_webhooks(ctx: dict[str, Any]) -> dict[str, Any]:
    sessionmaker: async_sessionmaker[AsyncSession] = ctx["sessionmaker"]
    dispatcher: WebhookDispatcher = ctx[DISPATCHER]
    totals = {
        "orgs": 0,
        "skipped": 0,
        "fanned_out": 0,
        "sent": 0,
        "succeeded": 0,
        "failed_attempts": 0,
    }
    disabled: list[str] = []
    errors: list[str] = []
    for org_id in await due_org_ids(sessionmaker):
        totals["orgs"] += 1
        try:
            stats = await dispatcher.run_org(org_id)
        except Exception as exc:
            errors.append(str(org_id))
            log.error("webhook_delivery_failed", org_id=str(org_id), error=type(exc).__name__)
            continue
        totals["skipped"] += int(stats.skipped)
        totals["fanned_out"] += stats.fanned_out
        totals["sent"] += stats.sent
        totals["succeeded"] += stats.succeeded
        totals["failed_attempts"] += stats.failed_attempts
        disabled.extend(stats.disabled_endpoints)
    summary: dict[str, Any] = {**totals, "disabled_endpoints": len(disabled), "errors": len(errors)}
    if totals["sent"] or errors or disabled:
        log.info("webhook_delivery_finished", **summary)
    return summary
