# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""The candidate experience survey (M3) without a database: the form, the link format
and the invitation email."""

from __future__ import annotations

import uuid

import pytest

from pairly_core.errors import BadRequestError
from pairly_core.notifications.defaults import DefaultTemplate
from pairly_core.notifications.templating import (
    TemplateSource,
    has_em_dash,
    lint_source,
    parse_declarations,
)
from pairly_hire.survey import form, service
from pairly_hire.survey.templates import SURVEY_TEMPLATES


def test_three_to_five_questions_in_both_languages() -> None:
    en, ar = form.questions_out("en"), form.questions_out("ar")
    assert 3 <= len(en) <= 5
    assert [q["id"] for q in en] == [q["id"] for q in ar]
    assert all(a["label"] != e["label"] for a, e in zip(ar, en, strict=True))
    assert not any(has_em_dash(q["label"]) for q in en + ar)


def test_answers_are_checked_and_the_comment_kept_apart() -> None:
    answers, comment = form.check_answers({"overall": 5, "recommend": "yes", "comment": "  ok  "})
    assert answers == {"overall": 5, "recommend": "yes"}
    assert comment == "ok"
    for bad in (
        {},
        {"overall": 0},
        {"overall": True},
        {"overall": 3, "recommend": "maybe"},
        {"overall": 3, "comment": "x" * 1001},
        {"overall": 3, "name": "me"},
    ):
        with pytest.raises(BadRequestError):
            form.check_answers(bad)


def test_link_format_is_strict() -> None:
    org, invite = uuid.uuid4(), uuid.uuid4()
    good = f"sv1.{org.hex}.{invite.hex}.{'A' * 43}"
    claim = service.parse(good)
    assert claim is not None
    assert claim.invite_id == invite
    for bad in (good.replace("sv1", "sv2"), good + "A", f"sv1.{org.hex}.{'A' * 43}", "x" * 300):
        assert service.parse(bad) is None
    assert service.TTL.days == 30


@pytest.mark.parametrize("template", SURVEY_TEMPLATES)
def test_survey_email_lints(template: DefaultTemplate) -> None:
    specs = parse_declarations(template.variables)
    source = TemplateSource(
        template.channel,  # type: ignore[arg-type]
        template.locale,  # type: ignore[arg-type]
        template.subject,
        template.body,
        template.body_html,
        specs,
    )
    assert [i for i in lint_source(source) if i.severity == "error"] == []
    assert not has_em_dash(template.body + (template.subject or ""))
