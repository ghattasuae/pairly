# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Scheduling without a database: messages, signed links, the written-out times."""

from __future__ import annotations

import time
import uuid
from collections import defaultdict
from datetime import UTC, datetime, timedelta

import pytest

from pairly_core.notifications.defaults import DefaultTemplate
from pairly_core.notifications.templating import (
    TemplateSource,
    has_em_dash,
    lint_source,
    parse_declarations,
    render_source,
    sample_variables,
)
from pairly_core.plugins import discover_plugins
from pairly_core.rbac import build_registry
from pairly_hire.scheduling.emails import SchedulingEnv, ics_uid, when_text
from pairly_hire.scheduling.models import HireInterview
from pairly_hire.scheduling.service import advisory_key
from pairly_hire.scheduling.templates import SCHEDULING_TEMPLATES
from pairly_hire.scheduling.tokens import (
    connect_state,
    new_nonce,
    read_connect_state,
    read_schedule_token,
    schedule_token,
)

SECRET = "x" * 48


def _source(t: DefaultTemplate) -> TemplateSource:
    return TemplateSource(
        t.channel,  # type: ignore[arg-type]
        t.locale,  # type: ignore[arg-type]
        t.subject,
        t.body,
        t.body_html,
        parse_declarations(t.variables),
    )


@pytest.mark.parametrize("template", SCHEDULING_TEMPLATES, ids=lambda t: f"{t.key}.{t.locale}")
def test_every_scheduling_template_lints_and_renders(template: DefaultTemplate) -> None:
    issues = lint_source(_source(template))
    assert [i for i in issues if i.severity == "error"] == []
    assert [i.code for i in issues if i.code == "unused_variable"] == []
    for text in (template.subject or "", template.body):
        assert not has_em_dash(text)
    rendered = render_source(_source(template), sample_variables(_source(template).variables))
    assert rendered.text
    assert not has_em_dash(rendered.text)
    assert template.category == "interviews"
    assert not template.requires_approval


def test_every_scheduling_template_has_english_and_arabic() -> None:
    seen: dict[tuple[str, str], set[str]] = defaultdict(set)
    for t in SCHEDULING_TEMPLATES:
        seen[(t.key, t.channel)].add(t.locale)
    assert all(locales == {"en", "ar"} for locales in seen.values()), seen


def test_schedule_token_binds_interview_application_stage_and_nonce() -> None:
    org, interview, app = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    nonce = new_nonce()
    expires = datetime.now(UTC) + timedelta(days=7)
    token = schedule_token(
        SECRET,
        org_id=org,
        interview_id=interview,
        application_id=app,
        stage_key="interview",
        nonce=nonce,
        expires_at=expires,
    )
    claim = read_schedule_token(SECRET, token)
    assert claim is not None
    assert (claim.org_id, claim.interview_id, claim.application_id) == (org, interview, app)
    assert (claim.stage_key, claim.nonce) == ("interview", nonce)
    # A changed stage, application or nonce breaks the signature.
    parts = token.split(".")
    for index, value in ((4, "offer"), (3, uuid.uuid4().hex), (5, new_nonce())):
        forged = parts.copy()
        forged[index] = value
        assert read_schedule_token(SECRET, ".".join(forged)) is None
    assert read_schedule_token("y" * 48, token) is None
    assert read_schedule_token(SECRET, token, now=expires.timestamp() + 1) is None
    assert read_schedule_token(SECRET, token + "x" * 300) is None
    with pytest.raises(ValueError, match="invalid"):
        schedule_token(
            SECRET,
            org_id=org,
            interview_id=interview,
            application_id=app,
            stage_key="Bad Stage",
            nonce=nonce,
            expires_at=expires,
        )


def test_connect_state_is_bound_to_the_person_and_expires() -> None:
    org, user = uuid.uuid4(), uuid.uuid4()
    state = connect_state(SECRET, org, user)
    claim = read_connect_state(SECRET, state)
    assert claim is not None
    assert (claim.org_id, claim.user_id) == (org, user)
    assert read_connect_state(SECRET, state, now=time.time() + 3600) is None
    assert read_connect_state(SECRET, state.replace(user.hex, uuid.uuid4().hex)) is None


def test_times_are_written_in_the_candidates_zone_and_language() -> None:
    start = datetime(2026, 10, 13, 6, 0, tzinfo=UTC)
    end = start + timedelta(minutes=45)
    assert when_text(start, end, "Asia/Dubai", "en") == (
        "Tuesday 13 October 2026, 10:00 to 10:45 (Asia/Dubai)"
    )
    assert when_text(start, end, "America/New_York", "en") == (
        "Tuesday 13 October 2026, 02:00 to 02:45 (America/New_York)"
    )
    assert when_text(start, end, "Asia/Dubai", "ar") == (
        "الثلاثاء 13 أكتوبر 2026، من 10:00 إلى 10:45 (Asia/Dubai)"
    )
    # Around the UK clock change: 25 October 2026 is GMT again.
    autumn = datetime(2026, 10, 26, 9, 0, tzinfo=UTC)
    assert "09:00 to 09:45 (Europe/London)" in when_text(
        autumn, autumn + timedelta(minutes=45), "Europe/London", "en"
    )


def test_identifiers_and_environment() -> None:
    interview_id = uuid.uuid4()
    assert ics_uid(interview_id) == f"iv-{interview_id.hex}@pairly"
    org, user = uuid.uuid4(), uuid.uuid4()
    assert advisory_key(org, user) == advisory_key(org, user)
    assert advisory_key(org, user) != advisory_key(org, uuid.uuid4())
    assert SchedulingEnv.from_settings(None, web_url="https://app.example/").links.web_url == (
        "https://app.example"
    )
    assert HireInterview.__tablename__ == "hire_interviews"


def test_plugin_declares_scheduling_permissions_flag_and_job() -> None:
    plugin = discover_plugins()["hire"]
    registry = build_registry([plugin])
    interviewer = registry.permissions_for_role("interviewer")
    assert "hire.interview.read" in interviewer
    assert "hire.interview.schedule" not in interviewer
    assert "hire.interview.schedule" in registry.permissions_for_role("recruiter")
    flags = {f.key: f.default for f in plugin.feature_flags}  # type: ignore[attr-defined]
    assert flags == {"hire.scheduling.google": False}
    jobs = {j.name: j.every_minutes for j in plugin.scheduled_jobs}  # type: ignore[attr-defined]
    assert jobs["hire_interview_reminders"] == 5
