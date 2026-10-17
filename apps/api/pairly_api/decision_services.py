# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Services the decision, appeal, compliance and audit hardening routes use.

Stored on ``app.state.decision_services`` by the API factory (tests pass their own):

* ``router``: the model router replay runs through (recorded responses by default).
* ``queue``: arq queue for the compliance export and large-chain verification jobs.
* ``anchor_public_key`` / ``anchor_store``: to check the chain against its anchored
  heads (``pairly_core.audit_anchor``). Required outside development and test.
* ``large_chain_events``: chains longer than this are verified by the worker.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field

import structlog
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from pairly_core.audit_anchor.anchoring import anchor_map, load_anchors, public_keys
from pairly_core.audit_anchor.signing import PUBLIC_KEY_ENV, load_public_key
from pairly_core.audit_anchor.store import (
    AnchorStore,
    anchor_store_from_settings,
    load_anchor_settings,
)
from pairly_core.audit_anchor.verification import LARGE_CHAIN_EVENTS, AnchorLoader
from pairly_core.config import ConfigError, Settings
from pairly_core.decisions.jobs import ArqTaskQueue, NullTaskQueue, TaskQueue
from pairly_core.decisions.replay import ReplayRouter

log = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class DecisionServices:
    router: ReplayRouter | None = None
    queue: TaskQueue = field(default_factory=NullTaskQueue)
    anchor_public_key: Ed25519PublicKey | None = None
    anchor_store: AnchorStore | None = None
    large_chain_events: int = LARGE_CHAIN_EVENTS

    def anchor_loader(self) -> AnchorLoader | None:
        """Loads an organisation's verified anchors, or None when anchors are not set up."""
        store, key = self.anchor_store, self.anchor_public_key
        if store is None or key is None:
            return None
        keys = public_keys(key)

        async def load(org_id: uuid.UUID) -> Mapping[int, str]:
            return anchor_map(await load_anchors(store, org_id, keys))

        return load


def get_decision_services(request: Request) -> DecisionServices:
    services = getattr(request.app.state, "decision_services", None)
    return services if isinstance(services, DecisionServices) else DecisionServices()


async def build_decision_services(
    settings: Settings, sessionmaker: async_sessionmaker[AsyncSession]
) -> DecisionServices:
    """The production wiring, from the environment. Fails closed outside dev and test."""
    from pairly_core.llm.factory import build_router
    from pairly_core.llm.settings import load_llm_settings

    router = build_router(load_llm_settings(), sessionmaker)
    key = load_public_key()
    if key is None and settings.is_production:
        raise ConfigError(f"missing required settings: {PUBLIC_KEY_ENV}")
    store: AnchorStore | None = None
    if key is not None:
        try:
            store = anchor_store_from_settings(load_anchor_settings())
        except ConfigError:
            if settings.is_production:
                raise
            log.warning("audit_anchors_disabled", reason="anchor storage settings missing")
    queue: TaskQueue = NullTaskQueue()
    redis_url = os.environ.get("PAIRLY_REDIS_URL", "").strip()
    if redis_url:
        try:
            queue = await ArqTaskQueue.connect(redis_url)
        except Exception as exc:  # the worker's sweeper still picks the jobs up
            log.warning("decision_queue_unavailable", error=type(exc).__name__)
    return DecisionServices(router=router, queue=queue, anchor_public_key=key, anchor_store=store)
