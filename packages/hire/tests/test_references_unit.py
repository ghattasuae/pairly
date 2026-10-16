# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Reference checks (M3) without a database: the form, the link format, the consistency
flags against the CV, the deterministic summary and the referee's email."""

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
from pairly_hire.references import form, links
from pairly_hire.references.consistency import CvJob, flags_for, same_employer
from pairly_hire.references.summary import check_draft, summary_inputs, template_draft
from pairly_hire.references.templates import REFEREE_TEMPLATES

FULL = {
    "known_as": "manager",
    "company": "Gulf Logistics LLC",
    "candidate_title": "Senior Backend Engineer",
    "worked_from": "2021-03",
    "worked_to": "2023-08",
    "strengths": "Calm, clear trade offs.",
    "development": "Delegation.",
    "reliability": 4,
    "would_rehire": "yes",
}
CV = [
    CvJob("Gulf Logistics", "Backend Engineer", "2020-01", "2023-09"),
    CvJob("Desert Bank", "Software Engineer", "2017-06", "2019-12"),
]


def test_form_saves_partial_drafts_and_checks_submissions() -> None:
    assert form.clean_answers({"company": "  Gulf  ", "strengths": ""}) == {"company": "Gulf"}
    assert form.check_answers(FULL)["reliability"] == 4
    with pytest.raises(BadRequestError, match="required"):
        form.check_answers({"company": "Gulf"})
    for bad in (
        {"reliability": 0},
        {"reliability": True},
        {"worked_from": "2021/03"},
        {"would_rehire": "maybe"},
        {"strengths": "x" * (form.MAX_TEXT + 1)},
        {"company": "a\x00b"},
        {"unknown": "x"},
    ):
        with pytest.raises(BadRequestError):
            form.clean_answers(bad)
    with pytest.raises(BadRequestError, match="after the start"):
        form.check_answers({**FULL, "worked_to": "2020-01"})
    en, ar = form.questions_out("en"), form.questions_out("ar")
    assert [q["id"] for q in en] == [q["id"] for q in ar]
    assert all(q["label"] != a["label"] for q, a in zip(en, ar, strict=True))
    assert not any(has_em_dash(q["label"]) for q in en + ar)


def test_link_format_is_strict() -> None:
    org, row, req = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    secret = "A" * 43
    good = f"rr1.{org.hex}.{row.hex}.{req.hex}.{secret}"
    claim = links.parse(links.LINK_PREFIX, good)
    assert claim is not None
    assert claim.request_id == req
    for bad in (
        good.replace("rr1", "rs1"),
        good + "A",
        good.replace(secret, "A" * 42 + "!"),
        f"rr1.{org.hex}.{row.hex}.{secret}",
        "x" * 300,
    ):
        assert links.parse(links.LINK_PREFIX, bad) is None
    assert links.LINK_TTL.total_seconds() <= 72 * 3600


def test_consistency_flags_compare_referees_with_the_cv() -> None:
    assert same_employer("Gulf Logistics LLC", "gulf logistics")
    assert not same_employer("Gulf Logistics", "Desert Bank")
    assert flags_for([("Referee 1", FULL)], CV) == []
    other = {**FULL, "company": "Oasis Telecom"}
    assert [f.kind for f in flags_for([("Referee 1", other)], CV)] == ["employer_not_on_cv"]
    title = {**FULL, "candidate_title": "Sales Manager"}
    assert [f.kind for f in flags_for([("Referee 1", title)], CV)] == ["title_differs"]
    early = {**FULL, "worked_from": "2018-01"}
    assert [f.kind for f in flags_for([("Referee 1", early)], CV)] == ["dates_differ"]
    still = {k: v for k, v in FULL.items() if k != "worked_to"}
    assert [f.kind for f in flags_for([("Referee 1", still)], CV)] == ["dates_differ"]
    low = {**FULL, "reliability": 1, "would_rehire": "no"}
    kinds = [f.kind for f in flags_for([("Referee 1", FULL), ("Referee 2", low)], CV)]
    assert kinds == ["would_not_rehire", "low_reliability", "referees_disagree"]
    assert [f.kind for f in flags_for([("Referee 1", FULL)], None)] == ["no_cv_profile"]
    flag = flags_for([("Referee 1", other)], CV)[0]
    assert "Referee 1" in flag.detail
    assert "Referee 1" in flag.detail_ar
    assert not has_em_dash(flag.detail + flag.detail_ar)


@pytest.mark.parametrize("locale", ["en", "ar"])
def test_template_summary_passes_its_checks(locale: str) -> None:
    low = {**FULL, "reliability": 1, "would_rehire": "no"}
    inputs = summary_inputs(
        job_title="Backend Engineer",
        locale=locale,
        references=[("Referee 1", FULL), ("Referee 2", low)],
    )
    draft = template_draft(inputs)
    assert check_draft(draft) == []
    assert len(draft.inconsistencies) == 2
    assert draft.strengths
    assert draft.development


@pytest.mark.parametrize("template", REFEREE_TEMPLATES)
def test_referee_email_lints(template: DefaultTemplate) -> None:
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


def test_demo_reference_recordings_are_up_to_date() -> None:
    from synth.hire import references as demo_references

    assert demo_references.main(["--check"]) == 0
