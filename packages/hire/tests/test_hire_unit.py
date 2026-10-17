# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Unit tests of the Hire plugin: permissions, rubric and form models, answers and
knockouts, the plugin's extension points, retention and the recordings bundle."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest

from pairly_core.errors import BadRequestError
from pairly_core.llm.bundle import parse_bundle
from pairly_core.plugins import discover_plugins, plugin_prompt_roots
from pairly_core.policy_versions import PolicyError
from pairly_core.rbac import (
    Actor,
    ActorKind,
    AgentPermissionError,
    EmployeePermissionError,
    RoleGrant,
    SecondFactorRequiredError,
    authorize,
    build_registry,
)
from pairly_core.retention.rules import DeleteMode, build_retention_registry
from pairly_hire import permissions as P  # noqa: N812
from pairly_hire.demo import BACKEND, DEMO_JOBS
from pairly_hire.drafting import JobDescriptionDraft
from pairly_hire.forms import (
    DEFAULT_FORM,
    ApplicationForm,
    check_answers,
    parse_form,
)
from pairly_hire.plugin import plugin
from pairly_hire.recordings import build_bundle, recordings_text
from pairly_hire.retention import RULE_KEY, retention_until
from pairly_hire.rubric import Rubric, parse_rubric, policy_name

REGISTRY = build_registry([plugin])


def _actor(role: str, *, otp: bool = True, kind: ActorKind = ActorKind.USER) -> Actor:
    grants = (RoleGrant(role, "hire"),)
    return Actor(
        kind=kind,
        id=uuid.uuid4(),
        org_id=uuid.uuid4(),
        grants=grants,
        permissions=REGISTRY.resolve(grants),
        amr=frozenset({"otp"}) if otp else frozenset(),
    )


# ------------------------------------------------------------------ permissions


@pytest.mark.parametrize(
    ("role", "allowed"),
    [
        ("recruiter", {P.JOB_READ, P.JOB_WRITE, P.JOB_PUBLISH, P.RUBRIC_LOCK, P.RUBRIC_WRITE}),
        ("hiring_manager", {P.JOB_READ, P.JOB_WRITE, P.RUBRIC_LOCK, P.RUBRIC_WRITE}),
        ("interviewer", {P.JOB_READ, P.RUBRIC_READ, P.CANDIDATE_READ}),
        ("employee", {P.JOB_READ}),
        ("admin", {P.JOB_PUBLISH, P.SELFID_READ, P.SETTINGS_MANAGE}),
        ("auditor", {P.SELFID_READ, P.CANDIDATE_READ}),
    ],
)
def test_default_grants(role: str, allowed: set[str]) -> None:
    assert allowed <= REGISTRY.permissions_for_role(role)


@pytest.mark.parametrize(
    ("role", "denied"),
    [
        ("employee", {P.JOB_WRITE, P.CANDIDATE_READ, P.RUBRIC_READ, P.SELFID_READ}),
        ("interviewer", {P.JOB_WRITE, P.RUBRIC_LOCK, P.SELFID_READ}),
        ("hiring_manager", {P.JOB_PUBLISH, P.SELFID_READ}),
        ("recruiter", {P.SELFID_READ, P.SETTINGS_MANAGE}),
        ("admin", {P.RUBRIC_LOCK}),
        ("agent", {P.JOB_WRITE, P.RUBRIC_LOCK, P.SELFID_READ, P.CANDIDATE_DECIDE}),
    ],
)
def test_who_never_holds_what(role: str, denied: set[str]) -> None:
    assert not denied & REGISTRY.permissions_for_role(role)


def test_agent_only_reads_and_proposes() -> None:
    held = REGISTRY.permissions_for_role("agent")
    actions = {p.rsplit(".", 1)[1] for p in held if p.startswith("hire.")}
    assert actions <= {"read", "propose"}


def test_employee_only_holds_submit_type_actions() -> None:
    held = {p for p in REGISTRY.permissions_for_role("employee") if p.startswith("hire.")}
    # Open jobs, and their own referral links with the stage of the people they referred.
    assert held == {P.JOB_READ, P.REFERRAL_CREATE, P.REFERRAL_READ}


def test_lock_and_publish_need_a_second_factor() -> None:
    for role, permission in (("recruiter", P.RUBRIC_LOCK), ("recruiter", P.JOB_PUBLISH)):
        authorize(_actor(role), permission)
        with pytest.raises(SecondFactorRequiredError):
            authorize(_actor(role, otp=False), permission)
    # Writing a draft does not.
    authorize(_actor("recruiter", otp=False), P.RUBRIC_WRITE)


def test_api_key_cannot_lock_or_publish() -> None:
    key = _actor("recruiter", kind=ActorKind.API_KEY)
    for permission in (P.RUBRIC_LOCK, P.JOB_PUBLISH, P.JOB_WRITE):
        with pytest.raises(Exception, match="missing permission"):
            authorize(key, permission)


def test_registry_refuses_bad_grants() -> None:
    from pairly_core.rbac import BuiltinRole, PermissionSpec, RoleRegistry

    registry = RoleRegistry()
    with pytest.raises(AgentPermissionError):
        registry.register(
            PermissionSpec("hire.rubric.lock", "x", frozenset({BuiltinRole.AGENT})),
            namespace="hire",
        )
    with pytest.raises(EmployeePermissionError):
        registry.register(
            PermissionSpec("hire.job.publish", "x", frozenset({BuiltinRole.EMPLOYEE})),
            namespace="hire",
        )


# ------------------------------------------------------------------ plugin


def test_plugin_is_discovered_with_its_extension_points() -> None:
    found = discover_plugins()["hire"]
    assert found.id == "hire"
    roots = plugin_prompt_roots([found])
    assert any((r / "hire_jd_draft" / "v1.md").is_file() for r in roots)
    assert any((r / "hire_rubric_draft" / "v1.md").is_file() for r in roots)
    assert RULE_KEY in build_retention_registry([found])
    assert "hire-demo" in found.demo_seeds  # type: ignore[attr-defined]
    assert (found.migrations_dir / "hire_0001_jobs_applications.py").is_file()  # type: ignore[attr-defined]


def test_retention_until_adds_calendar_months() -> None:
    start = datetime(2026, 1, 31, 12, tzinfo=UTC)
    assert retention_until(start, 1) == datetime(2026, 2, 28, 12, tzinfo=UTC)
    assert retention_until(start, 12) == datetime(2027, 1, 31, 12, tzinfo=UTC)
    rule = next(r for r in plugin.retention_rules if r.key == RULE_KEY)
    # Candidates go one by one through the erasure; the period is Hire's months setting.
    assert rule.mode is DeleteMode.SUBJECTS
    assert rule.subjects is not None
    assert rule.subjects.subject_type == "hire.candidate"
    assert rule.period is not None
    assert (rule.period.unit, rule.period.default) == ("months", 12)


# ------------------------------------------------------------------ rubric


def test_rubric_round_trips_through_yaml() -> None:
    text = BACKEND.rubric.to_yaml()
    assert parse_rubric(text) == BACKEND.rubric
    assert "—" not in text


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"must_haves": []}, "must_haves"),
        ({"schema_version": 2}, "schema_version"),
        ({"extra": 1}, "extra"),
    ],
)
def test_rubric_validation(change: dict[str, Any], message: str) -> None:
    data = BACKEND.rubric.model_dump(mode="json") | change
    with pytest.raises(PolicyError, match=message):
        parse_rubric(json.dumps(data))


def test_rubric_ids_unique_and_weights_bounded() -> None:
    data = BACKEND.rubric.model_dump(mode="json")
    data["nice_to_haves"][0]["id"] = data["must_haves"][0]["id"]
    with pytest.raises(PolicyError, match="unique"):
        parse_rubric(json.dumps(data))
    data = BACKEND.rubric.model_dump(mode="json")
    data["must_haves"][0]["weight"] = 11
    with pytest.raises(PolicyError):
        parse_rubric(json.dumps(data))


def test_rubric_policy_name() -> None:
    job = uuid.uuid4()
    assert policy_name(job.hex) == f"hire.job.{job.hex}"


# ------------------------------------------------------------------ forms and answers


def test_default_form_round_trips() -> None:
    assert parse_form(DEFAULT_FORM.to_yaml()) == DEFAULT_FORM
    assert DEFAULT_FORM.file_ids == {"cv"}
    assert DEFAULT_FORM.self_identification.active == ("gender", "age_range", "disability")


def test_form_validation() -> None:
    with pytest.raises(ValueError, match="at least two options"):
        ApplicationForm.model_validate(
            {"questions": [{"id": "q", "kind": "choice", "label": "Pick", "options": []}]}
        )
    with pytest.raises(ValueError, match="not 'cv'"):
        ApplicationForm.model_validate({"files": [{"id": "cv", "label": "Again"}]})
    with pytest.raises(ValueError, match="unknown self-identification"):
        ApplicationForm.model_validate({"self_identification": {"categories": ["religion"]}})


FORM = ApplicationForm.model_validate(
    {
        "questions": [
            {"id": "notice", "kind": "text", "label": "Notice period", "required": True},
            {
                "id": "level",
                "kind": "choice",
                "label": "Level",
                "options": [{"id": "mid", "label": "Mid"}, {"id": "senior", "label": "Senior"}],
            },
            {"id": "relocate", "kind": "yes_no", "label": "Relocate?"},
        ]
    }
)


def test_knockout_mismatch_goes_to_a_person_never_rejects() -> None:
    checked = check_answers(
        FORM,
        BACKEND.rubric,
        {"notice": " 30 days ", "level": "senior"},
        {"right_to_work_uae": "no"},
    )
    assert checked.questions == {"notice": "30 days", "level": "senior"}
    assert checked.review_reasons == ["knockout:right_to_work_uae"]
    assert checked.stored()["knockouts"] == {"right_to_work_uae": "no"}
    matching = check_answers(FORM, BACKEND.rubric, {"notice": "now"}, {"right_to_work_uae": "yes"})
    assert matching.review_reasons == []


@pytest.mark.parametrize(
    ("questions", "knockouts", "message"),
    [
        ({}, {"right_to_work_uae": "yes"}, "notice needs an answer"),
        ({"notice": "x", "level": "junior"}, {"right_to_work_uae": "yes"}, "one of its options"),
        ({"notice": "x", "relocate": "maybe"}, {"right_to_work_uae": "yes"}, "yes or no"),
        ({"notice": "x", "other": "y"}, {"right_to_work_uae": "yes"}, "does not ask"),
        ({"notice": "x"}, {}, "knockout question right_to_work_uae"),
        ({"notice": "x"}, {"right_to_work_uae": "yes", "made_up": "no"}, "does not ask"),
        ({"notice": "x" * 1001}, {"right_to_work_uae": "yes"}, "longer than"),
    ],
)
def test_answer_validation(
    questions: dict[str, Any], knockouts: dict[str, Any], message: str
) -> None:
    with pytest.raises(BadRequestError, match=message):
        check_answers(FORM, BACKEND.rubric, questions, knockouts)


# ------------------------------------------------------------------ recordings


def test_recordings_bundle_is_current() -> None:
    assert recordings_text() == build_bundle(), "run: uv run python -m pairly_hire.recordings"


def test_recordings_cover_every_demo_draft() -> None:
    entries = parse_bundle(recordings_text())
    tasks = sorted(e["task"] for e in entries)
    assert tasks == sorted(["hire_jd_draft", "hire_rubric_draft"] * len(DEMO_JOBS))
    for entry in entries:
        if entry["task"] == "hire_jd_draft":
            JobDescriptionDraft.model_validate(entry["response"])
        else:
            Rubric.model_validate(entry["response"])


def test_demo_text_has_no_em_dash_and_passes_its_own_checks() -> None:
    from pairly_hire.drafting import check_texts

    for job in DEMO_JOBS:
        blob = json.dumps(job.draft.model_dump(), ensure_ascii=False) + job.rubric.to_yaml()
        assert "—" not in blob
        checks = check_texts(
            title=job.draft.title,
            title_ar=job.draft.title_ar,
            description_en=job.draft.description_en,
            description_ar=job.draft.description_ar,
        )
        assert checks.inclusive_en == []
        assert checks.inclusive_ar == []
        assert checks.sanity_en == []


def test_job_recruiters_are_the_owner_and_the_co_recruiters() -> None:
    from pairly_hire.models import HireJob
    from pairly_hire.recruiters import job_recruiters, owner_of

    creator, owner, co = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    seeded = HireJob(created_by=None, owner_id=None, co_recruiter_ids=[])
    assert job_recruiters(seeded) == set()
    legacy = HireJob(created_by=creator, owner_id=None, co_recruiter_ids=[])
    assert owner_of(legacy) == creator
    assert job_recruiters(legacy) == {creator}
    assigned = HireJob(created_by=creator, owner_id=owner, co_recruiter_ids=[co])
    # The creator stops hearing about the job once someone else owns it.
    assert job_recruiters(assigned) == {owner, co}
