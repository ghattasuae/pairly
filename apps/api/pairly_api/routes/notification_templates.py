# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Notification templates: list, versions, save a version (linted), preview (WP1.9).

* ``GET  /v1/notification-templates``                 the version each key would use
* ``GET  /v1/notification-templates/{key}/versions``  every version of a key
* ``POST /v1/notification-templates``                 save a new version (lint first)
* ``POST /v1/notification-templates/preview``         render a draft or a saved version
                                                       with sample values; never sends

Reading and previewing need ``core.template.read``; saving needs
``core.template.manage`` (step-up). A save with lint errors answers 400
``template_invalid`` with every issue in ``issues``.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from pairly_api.problems import problem_response
from pairly_core.errors import BadRequestError
from pairly_core.identity import OrgSession
from pairly_core.notifications.models import Template
from pairly_core.notifications.permissions import TEMPLATE_MANAGE, TEMPLATE_READ
from pairly_core.notifications.templates import (
    TemplateLintError,
    create_template_version,
    get_template,
    latest_templates,
    preview,
    source_of,
    template_versions,
)
from pairly_core.notifications.templating import (
    TemplateRenderError,
    TemplateSource,
    parse_declarations,
)
from pairly_core.rbac import Actor, require

router = APIRouter(prefix="/notification-templates", tags=["notifications"])

Reader = Annotated[Actor, Depends(require(TEMPLATE_READ))]
Manager = Annotated[Actor, Depends(require(TEMPLATE_MANAGE))]
Key = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*){1,4}$", max_length=96)]


class TemplateOut(BaseModel):
    id: uuid.UUID
    key: str
    channel: str
    locale: str
    version: int
    category: str
    system: bool
    subject: str | None
    body: str
    body_html: str | None
    variables: dict[str, Any]
    requires_approval: bool
    created_at: datetime


def template_out(row: Template) -> TemplateOut:
    return TemplateOut(
        id=row.id,
        key=row.key,
        channel=row.channel,
        locale=row.locale,
        version=row.version,
        category=row.category,
        system=row.org_id is None,
        subject=row.subject,
        body=row.body,
        body_html=row.body_html,
        variables=row.variables,
        requires_approval=row.requires_approval,
        created_at=row.created_at,
    )


class IssueOut(BaseModel):
    severity: str
    code: str
    message: str
    field: str | None


class TemplateIn(BaseModel):
    key: Key
    channel: Literal["email", "slack", "in_app"]
    locale: Literal["en", "ar"]
    category: Annotated[str | None, Field(pattern=r"^[a-z][a-z0-9_]{0,31}$")] = None
    subject: Annotated[str | None, Field(max_length=500)] = None
    body: Annotated[str, Field(min_length=1, max_length=20_000)]
    body_html: Annotated[str | None, Field(max_length=20_000)] = None
    variables: dict[str, dict[str, Any]] = Field(default_factory=dict)
    requires_approval: bool = False


class SavedOut(BaseModel):
    template: TemplateOut
    issues: list[IssueOut]


class PreviewIn(BaseModel):
    template_id: uuid.UUID | None = None
    channel: Literal["email", "slack", "in_app"] | None = None
    locale: Literal["en", "ar"] = "en"
    subject: Annotated[str | None, Field(max_length=500)] = None
    body: Annotated[str | None, Field(max_length=20_000)] = None
    body_html: Annotated[str | None, Field(max_length=20_000)] = None
    variables: dict[str, dict[str, Any]] = Field(default_factory=dict)
    values: dict[str, Any] = Field(default_factory=dict)
    timezone: Annotated[str | None, Field(max_length=64)] = None


class PreviewOut(BaseModel):
    subject: str | None
    text: str | None
    html: str | None
    direction: str
    issues: list[IssueOut]
    values: dict[str, Any]


@router.get("", response_model=list[TemplateOut])
async def list_templates(actor: Reader, session: OrgSession) -> list[TemplateOut]:
    return [template_out(t) for t in await latest_templates(session)]


@router.get("/{key}/versions", response_model=list[TemplateOut])
async def list_versions(key: str, actor: Reader, session: OrgSession) -> list[TemplateOut]:
    return [template_out(t) for t in await template_versions(session, key)]


@router.post("", response_model=SavedOut, status_code=201)
async def save_template(
    body: TemplateIn, request: Request, actor: Manager, session: OrgSession
) -> SavedOut | JSONResponse:
    try:
        saved = await create_template_version(
            session,
            actor,
            key=body.key,
            channel=body.channel,
            locale=body.locale,
            category=body.category,
            subject=body.subject,
            body=body.body,
            body_html=body.body_html,
            variables=body.variables,
            requires_approval=body.requires_approval,
        )
    except TemplateLintError as exc:
        return problem_response(
            request,
            exc.status,
            exc.code,
            title=exc.title,
            detail=exc.detail,
            issues=[i.to_json() for i in exc.issues],
        )
    return SavedOut(
        template=template_out(saved.template),
        issues=[IssueOut(**i.to_json()) for i in saved.issues],
    )


@router.post("/preview", response_model=PreviewOut)
async def preview_template(body: PreviewIn, actor: Reader, session: OrgSession) -> PreviewOut:
    if body.template_id is not None:
        source = source_of(await get_template(session, body.template_id))
    else:
        if body.channel is None or body.body is None:
            raise BadRequestError("give a template_id, or a channel and a body")
        try:
            specs = parse_declarations(body.variables)
        except TemplateRenderError as exc:
            raise BadRequestError(str(exc)) from None
        source = TemplateSource(
            channel=body.channel,
            locale=body.locale,
            subject=body.subject,
            body=body.body,
            body_html=body.body_html,
            variables=specs,
        )
    result = preview(source, body.values, tz=body.timezone)
    rendered = result.rendered
    return PreviewOut(
        subject=rendered.subject if rendered else None,
        text=rendered.text if rendered else None,
        html=rendered.html if rendered else None,
        direction="rtl" if source.locale == "ar" else "ltr",
        issues=[IssueOut(**i.to_json()) for i in result.issues],
        values={
            k: v if not isinstance(v, datetime) else v.isoformat()
            for k, v in result.variables.items()
        },
    )
