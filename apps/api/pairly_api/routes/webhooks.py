# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""``/v1/webhooks``: outbound webhook endpoints (WP1.11).

* ``GET   /v1/webhooks/event-types``                    the registered event types
* ``POST  /v1/webhooks/endpoints``                      create; returns the signing
  secret once (``core.webhooks.manage``, step-up)
* ``GET   /v1/webhooks/endpoints``                      list (``core.webhooks.read``;
                                                         the full URL only with
                                                         ``core.webhooks.manage``)
* ``GET   /v1/webhooks/endpoints/{id}``                 one
* ``PATCH /v1/webhooks/endpoints/{id}``                 change URL, event types,
  description, or enable and disable it
* ``POST  /v1/webhooks/endpoints/{id}/rotate-secret``   new signing secret, returned once
* ``GET   /v1/webhooks/endpoints/{id}/deliveries``      recent deliveries and their outcome

Endpoint URLs must be https and resolve only to public addresses (checked here and again
at every delivery). Secrets are never returned except at creation and rotation.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated
from urllib.parse import urlsplit, urlunsplit

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field

from pairly_core.identity import OrgSession
from pairly_core.rbac import Actor, require
from pairly_core.webhooks.events import event_ref, event_registry
from pairly_core.webhooks.models import WebhookDelivery, WebhookEndpoint
from pairly_core.webhooks.permissions import WEBHOOKS_MANAGE, WEBHOOKS_READ
from pairly_core.webhooks.service import (
    create_endpoint,
    get_endpoint,
    list_deliveries,
    list_endpoints,
    rotate_secret,
    update_endpoint,
)
from pairly_core.webhooks.settings import WebhookSettings, load_webhook_settings
from pairly_core.webhooks.ssrf import Resolver, system_resolver

router = APIRouter(prefix="/webhooks", tags=["webhooks"])

ReadActor = Annotated[Actor, Depends(require(WEBHOOKS_READ))]
ManageActor = Annotated[Actor, Depends(require(WEBHOOKS_MANAGE))]
EventTypeName = Annotated[str, Field(min_length=3, max_length=100)]


def webhook_settings(request: Request) -> WebhookSettings:
    settings = getattr(request.app.state, "webhook_settings", None)
    if not isinstance(settings, WebhookSettings):
        settings = load_webhook_settings()
        request.app.state.webhook_settings = settings
    return settings


def webhook_resolver(request: Request) -> Resolver:
    """The DNS resolver for URL checks (``app.state.webhook_resolver`` in tests)."""
    resolver = getattr(request.app.state, "webhook_resolver", None)
    return resolver if resolver is not None else system_resolver


Settings = Annotated[WebhookSettings, Depends(webhook_settings)]
ResolverDep = Annotated[Resolver, Depends(webhook_resolver)]


class EventTypeOut(BaseModel):
    key: str
    description: str


class EndpointIn(BaseModel):
    url: Annotated[str, Field(min_length=8, max_length=2048)]
    event_types: Annotated[list[EventTypeName], Field(min_length=1, max_length=100)]
    description: Annotated[str, Field(max_length=200)] | None = None


class EndpointPatch(BaseModel):
    url: Annotated[str, Field(min_length=8, max_length=2048)] | None = None
    event_types: Annotated[list[EventTypeName], Field(min_length=1, max_length=100)] | None = None
    description: Annotated[str, Field(max_length=200)] | None = None
    active: bool | None = None


def display_url(url: str) -> str:
    """Scheme, host and path only: no user info, port, query or fragment.

    Endpoint URLs often carry a token in the query string or the user info; people who
    may only read the endpoint list never see those parts.
    """
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.hostname or "", parts.path, "", ""))


class EndpointOut(BaseModel):
    id: uuid.UUID
    # The full URL for managers (core.webhooks.manage); scheme, host and path otherwise.
    url: str
    url_redacted: bool = False
    description: str | None
    event_types: list[str]
    active: bool
    consecutive_failures: int
    disabled_at: datetime | None
    disabled_reason: str | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def of(cls, row: WebhookEndpoint, *, full_url: bool = True) -> EndpointOut:
        return cls(
            id=row.id,
            url=row.url if full_url else display_url(row.url),
            url_redacted=not full_url,
            description=row.description,
            event_types=list(row.event_types),
            active=row.active,
            consecutive_failures=row.consecutive_failures,
            disabled_at=row.disabled_at,
            disabled_reason=row.disabled_reason,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )


class EndpointSecretOut(EndpointOut):
    # The signing secret. Shown once: store it now.
    secret: str


class DeliveryOut(BaseModel):
    id: uuid.UUID
    event_id: str
    event_type: str
    status: str
    attempts: int
    next_attempt_at: datetime
    last_attempt_at: datetime | None
    response_code: int | None
    latency_ms: int | None
    last_error: str | None
    created_at: datetime
    completed_at: datetime | None

    @classmethod
    def of(cls, row: WebhookDelivery) -> DeliveryOut:
        return cls(
            id=row.id,
            event_id=event_ref(row.event_id),
            event_type=row.event_type,
            status=row.status,
            attempts=row.attempts,
            next_attempt_at=row.next_attempt_at,
            last_attempt_at=row.last_attempt_at,
            response_code=row.response_code,
            latency_ms=row.latency_ms,
            last_error=row.last_error,
            created_at=row.created_at,
            completed_at=row.completed_at,
        )


@router.get("/event-types", response_model=list[EventTypeOut])
async def event_types(actor: ReadActor) -> list[EventTypeOut]:
    return [EventTypeOut(key=e.key, description=e.description) for e in event_registry().events]


@router.post("/endpoints", status_code=201, response_model=EndpointSecretOut)
async def create(
    body: EndpointIn,
    actor: ManageActor,
    session: OrgSession,
    settings: Settings,
    resolver: ResolverDep,
) -> EndpointSecretOut:
    created = await create_endpoint(
        session,
        actor,
        url=body.url,
        event_types=body.event_types,
        description=body.description,
        settings=settings,
        resolver=resolver,
    )
    await session.refresh(created.endpoint)
    return EndpointSecretOut(**EndpointOut.of(created.endpoint).model_dump(), secret=created.secret)


@router.get("/endpoints", response_model=list[EndpointOut])
async def list_all(actor: ReadActor, session: OrgSession) -> list[EndpointOut]:
    full = actor.has(WEBHOOKS_MANAGE)
    return [EndpointOut.of(row, full_url=full) for row in await list_endpoints(session)]


@router.get("/endpoints/{endpoint_id}", response_model=EndpointOut)
async def get_one(endpoint_id: uuid.UUID, actor: ReadActor, session: OrgSession) -> EndpointOut:
    row = await get_endpoint(session, endpoint_id)
    return EndpointOut.of(row, full_url=actor.has(WEBHOOKS_MANAGE))


@router.patch("/endpoints/{endpoint_id}", response_model=EndpointOut)
async def patch(
    endpoint_id: uuid.UUID,
    body: EndpointPatch,
    actor: ManageActor,
    session: OrgSession,
    settings: Settings,
    resolver: ResolverDep,
) -> EndpointOut:
    row = await update_endpoint(
        session,
        actor,
        endpoint_id,
        url=body.url,
        event_types=body.event_types,
        description=body.description,
        active=body.active,
        settings=settings,
        resolver=resolver,
    )
    return EndpointOut.of(row)


@router.post("/endpoints/{endpoint_id}/rotate-secret", response_model=EndpointSecretOut)
async def rotate(
    endpoint_id: uuid.UUID, actor: ManageActor, session: OrgSession
) -> EndpointSecretOut:
    rotated = await rotate_secret(session, actor, endpoint_id)
    return EndpointSecretOut(**EndpointOut.of(rotated.endpoint).model_dump(), secret=rotated.secret)


@router.get("/endpoints/{endpoint_id}/deliveries", response_model=list[DeliveryOut])
async def deliveries(
    endpoint_id: uuid.UUID,
    actor: ReadActor,
    session: OrgSession,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[DeliveryOut]:
    return [DeliveryOut.of(row) for row in await list_deliveries(session, endpoint_id, limit=limit)]
