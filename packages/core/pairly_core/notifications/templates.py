# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Template versions: save (with lint), resolve for sending, list and preview.

Saving (``create_template_version``) needs ``core.template.manage`` (a step-up
permission). The new version is linted first; any error refuses the save:

* syntax outside the allowed subset (``pairly_core.notifications.templating``),
* a variable used but not declared, a list loop over a non-list variable,
* an em dash anywhere (copy style), a missing subject on email, HTML on non-email.

Warnings do not block: declared but unused variables, an English version with no Arabic
version of the same key and channel, and a ``requires_approval`` flag that could not
be cleared. Two rules are sticky, so an organisation version can never weaken a
default: once any version of a key (the system default included) requires approval,
every new version does; and a key's category is the one of its system default.

Resolution (``resolve_template``) prefers the requested locale over English, the
organisation's own version over the system default, then the highest version.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy import case, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from pairly_core.audit import audit_actor_action
from pairly_core.errors import BadRequestError, ConflictError, NotFoundError
from pairly_core.notifications.models import Template
from pairly_core.notifications.templating import (
    CHANNELS,
    LOCALES,
    LintIssue,
    Rendered,
    TemplateRenderError,
    TemplateSource,
    declarations_json,
    lint_source,
    parse_declarations,
    render_source,
    sample_variables,
)
from pairly_core.rbac import Actor, authorize

MANAGE = "core.template.manage"
READ = "core.template.read"


class TemplateActions:
    VERSION_CREATED = "core.template.version_created"


class TemplateNotFoundError(NotFoundError):
    code = "template_not_found"
    title = "Template not found"


class TemplateLintError(BadRequestError):
    code = "template_invalid"
    title = "The template has errors"

    def __init__(self, issues: list[LintIssue]) -> None:
        errors = [i for i in issues if i.severity == "error"]
        super().__init__(errors[0].message if errors else self.title)
        self.issues = issues


def source_of(row: Template) -> TemplateSource:
    """The renderable parts of a stored version. Re-validates the declarations."""
    try:
        specs = parse_declarations(row.variables)
    except TemplateRenderError as exc:
        raise TemplateRenderError(f"stored template {row.key} is invalid: {exc}") from None
    if row.channel not in CHANNELS or row.locale not in LOCALES:
        raise TemplateRenderError("stored template has an unknown channel or locale")
    return TemplateSource(
        channel=row.channel,  # type: ignore[arg-type]
        locale=row.locale,  # type: ignore[arg-type]
        subject=row.subject,
        body=row.body,
        body_html=row.body_html,
        variables=specs,
    )


async def resolve_template(
    session: AsyncSession, *, key: str, channel: str, locale: str
) -> Template | None:
    """The version to send. Shared (system) rows and this organisation's rows only (RLS)."""
    locales = [locale, "en"] if locale != "en" else ["en"]
    query = (
        select(Template)
        .where(Template.key == key, Template.channel == channel, Template.locale.in_(locales))
        .order_by(
            case((Template.locale == locale, 0), else_=1),
            case((Template.org_id.is_(None), 1), else_=0),
            Template.version.desc(),
        )
        .limit(1)
    )
    return await session.scalar(query)


@dataclass(frozen=True, slots=True)
class SavedTemplate:
    template: Template
    issues: list[LintIssue]


async def create_template_version(
    session: AsyncSession,
    actor: Actor,
    *,
    key: str,
    channel: str,
    locale: str,
    category: str | None,
    subject: str | None,
    body: str,
    body_html: str | None,
    variables: Mapping[str, Any],
    requires_approval: bool,
) -> SavedTemplate:
    authorize(actor, MANAGE)
    if channel not in CHANNELS or locale not in LOCALES:
        raise BadRequestError("unknown channel or locale")
    try:
        specs = parse_declarations(variables)
    except TemplateRenderError as exc:
        raise TemplateLintError([LintIssue("error", "variables", str(exc))]) from None
    source = TemplateSource(
        channel=channel,  # type: ignore[arg-type]
        locale=locale,  # type: ignore[arg-type]
        subject=subject,
        body=body,
        body_html=body_html,
        variables=specs,
    )
    issues = lint_source(source)

    existing = list(await session.scalars(select(Template).where(Template.key == key)))
    system = [t for t in existing if t.org_id is None]
    if system:
        fixed_category = system[0].category
        if category is not None and category != fixed_category:
            issues.append(
                LintIssue(
                    "error",
                    "category_fixed",
                    f"the category of {key} is {fixed_category}",
                    "category",
                )
            )
        category = fixed_category
    elif category is None:
        issues.append(LintIssue("error", "category_required", "choose a category", "category"))
    if any(t.requires_approval for t in existing) and not requires_approval:
        requires_approval = True
        issues.append(
            LintIssue(
                "warning",
                "requires_approval_kept",
                "this template needs approval before sending, and that cannot be turned off",
                "requires_approval",
            )
        )
    if locale == "en" and not any(t.channel == channel and t.locale == "ar" for t in existing):
        issues.append(
            LintIssue("warning", "missing_ar", "there is no Arabic version of this template")
        )
    if any(i.severity == "error" for i in issues):
        raise TemplateLintError(issues)

    current = await session.scalar(
        select(func.max(Template.version)).where(
            Template.key == key,
            Template.channel == channel,
            Template.locale == locale,
            Template.org_id == actor.org_id,
        )
    )
    row = Template(
        id=uuid.uuid4(),
        org_id=actor.org_id,
        key=key,
        channel=channel,
        locale=locale,
        version=int(current or 0) + 1,
        category=category or "",
        subject=subject,
        body=body,
        body_html=body_html,
        variables=declarations_json(specs),
        requires_approval=requires_approval,
        created_by_type="user",
        created_by_id=str(actor.id),
    )
    session.add(row)
    try:
        async with session.begin_nested():
            await session.flush()
    except IntegrityError:
        raise ConflictError("another version was saved at the same time; try again") from None
    await audit_actor_action(
        session,
        actor,
        TemplateActions.VERSION_CREATED,
        entity_type="template",
        entity_id=row.id,
        payload={
            "key": key,
            "channel": channel,
            "locale": locale,
            "version": row.version,
            "category": row.category,
            "requires_approval": requires_approval,
            "warnings": [i.code for i in issues if i.severity == "warning"],
        },
    )
    return SavedTemplate(row, issues)


async def latest_templates(session: AsyncSession) -> list[Template]:
    """The version each (key, channel, locale) would send with, for this organisation."""
    rows = list(
        await session.scalars(
            select(Template).order_by(
                Template.key,
                Template.channel,
                Template.locale,
                case((Template.org_id.is_(None), 1), else_=0),
                Template.version.desc(),
            )
        )
    )
    seen: set[tuple[str, str, str]] = set()
    latest: list[Template] = []
    for row in rows:
        ident = (row.key, row.channel, row.locale)
        if ident not in seen:
            seen.add(ident)
            latest.append(row)
    return latest


async def template_versions(session: AsyncSession, key: str) -> list[Template]:
    return list(
        await session.scalars(
            select(Template)
            .where(Template.key == key)
            .order_by(Template.channel, Template.locale, Template.org_id, Template.version.desc())
        )
    )


async def get_template(session: AsyncSession, template_id: uuid.UUID) -> Template:
    row = await session.scalar(select(Template).where(Template.id == template_id))
    if row is None:
        raise TemplateNotFoundError()
    return row


@dataclass(frozen=True, slots=True)
class Preview:
    rendered: Rendered | None
    issues: list[LintIssue]
    variables: dict[str, Any]


def preview(
    source: TemplateSource,
    values: Mapping[str, Any] | None = None,
    *,
    tz: str | None = None,
    org_name: str | None = None,
) -> Preview:
    """Lint and render with sample values (or the given ones). Never sends anything."""
    issues = lint_source(source)
    sample = dict(sample_variables(source.variables))
    if values:
        sample.update(values)
    if any(i.severity == "error" for i in issues):
        return Preview(None, issues, sample)
    try:
        rendered = render_source(source, sample, tz=tz, org_name=org_name)
    except TemplateRenderError as exc:
        issues = [*issues, LintIssue("error", "render_failed", str(exc))]
        return Preview(None, issues, sample)
    return Preview(rendered, issues, sample)
