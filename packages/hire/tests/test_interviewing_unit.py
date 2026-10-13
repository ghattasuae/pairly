# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Interviews (M3) without a database: the plan model, kit inputs and checks, the
recordings bundle and the interviewer templates."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from pairly_core.notifications.templating import (
    TemplateSource,
    has_em_dash,
    lint_source,
    parse_declarations,
)
from pairly_hire.demo import BACKEND
from pairly_hire.interviewing import kits
from pairly_hire.interviewing import recordings as interview_recordings
from pairly_hire.interviewing.plan import (
    DEFAULT_ANCHORS,
    DEFAULT_ANCHORS_AR,
    InterviewPlan,
    PlanQuestion,
    default_plan,
)
from pairly_hire.interviewing.templates import BRIEFING_V2, SCORECARD_REMINDERS


def test_default_plan_maps_three_to_five_rubric_criteria() -> None:
    plan = default_plan(BACKEND.rubric)
    (stage,) = plan.stages
    ids = {c.id for c in [*BACKEND.rubric.must_haves, *BACKEND.rubric.nice_to_haves]}
    assert 3 <= len(stage.questions) <= 5
    assert set(stage.criteria()) <= ids
    assert stage.questions[0].scale() == DEFAULT_ANCHORS
    assert stage.questions[0].scale("ar") == DEFAULT_ANCHORS_AR
    assert len(DEFAULT_ANCHORS) == 4


def test_plan_validation() -> None:
    q = {"criterion_id": "python", "question": "Tell me about it."}
    with pytest.raises(ValidationError):
        InterviewPlan.model_validate(
            {"stages": [{"key": "interview", "name": "I", "questions": [q, q, q]}]}
        )
    with pytest.raises(ValidationError):
        PlanQuestion.model_validate(q | {"anchors": ["only one"]})
    stage = {
        "key": "interview",
        "name": "Interview",
        "questions": [q | {"criterion_id": c} for c in ("a", "b", "c")],
    }
    with pytest.raises(ValidationError):
        InterviewPlan.model_validate({"stages": [stage, stage]})
    assert InterviewPlan.model_validate({"stages": [stage]}).sla_hours == 24


def test_kit_inputs_carry_no_candidate_data() -> None:
    criterion = BACKEND.rubric.must_haves[0]
    inputs = kits.kit_inputs(
        job_title="Senior Backend Engineer",
        stage="Interview",
        locale="en",
        criterion=criterion,
        plan_question="Walk me through it.",
        result="partly_met",
    )
    assert set(inputs) == {
        "job_title",
        "stage",
        "language",
        "criterion",
        "plan_question",
        "screening_result",
    }
    assert set(inputs["criterion"]) == {"name", "description", "evidence"}


@pytest.mark.parametrize(
    "text",
    [
        "Ask about the project — in detail.",
        "Send it to layla@example.test please.",
        "See https://example.test/cv for more.",
        "Call +971 50 123 4567 after.",
        "How old are your children now?",
        "Does your family support the move?",
    ],
)
def test_kit_check_refuses_unsafe_text(text: str) -> None:
    draft = kits.KitItemDraft(questions=[text], probes=[], listen_for="")
    assert kits.check_item(draft)


def test_scripted_items_pass_the_checks_in_both_languages() -> None:
    criterion = BACKEND.rubric.must_haves[0]
    for locale in ("en", "ar"):
        for result in kits.RESULTS:
            inputs = kits.kit_inputs(
                job_title="Senior Backend Engineer",
                stage="Interview",
                locale=locale,
                criterion=criterion,
                plan_question="Walk me through a recent example.",
                result=result,
            )
            item = kits.scripted_item(inputs)
            assert kits.check_item(item) == []
            assert bool(item.probes) == (result != "not_screened")


def test_interview_recordings_are_up_to_date() -> None:
    assert interview_recordings.main(["--check"]) == 0


@pytest.mark.parametrize("template", [*BRIEFING_V2, *SCORECARD_REMINDERS])
def test_interviewer_templates_lint(template: object) -> None:
    from pairly_core.notifications.defaults import DefaultTemplate

    assert isinstance(template, DefaultTemplate)
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
    assert not has_em_dash(template.body)
    assert not has_em_dash(template.subject or "")
