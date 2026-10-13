# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology

"""Pipeline and messaging without a database (WP2.5, WP2.7).

* The template library: every template lints clean (no em dash, every variable
  declared and used), has English and Arabic, and the sensitive ones need approval.
* AI drafts: the recorded responses are current, each one only fills the personal
  paragraph and passes the fact check; drafts that add facts are refused.
* Stages: defaults, validation, next stage.
* Signed links: preferences and referral tokens.
* The plugin declares the message graph, its jobs and the rediscovery search kind.
"""

from __future__ import annotations

import json
import time
import uuid
from collections import defaultdict

import pytest

from pairly_core.errors import BadRequestError
from pairly_core.llm.bundle import parse_bundle
from pairly_core.notifications.defaults import SENSITIVE, DefaultTemplate
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
from pairly_hire import links
from pairly_hire.demo import BACKEND
from pairly_hire.messaging import library
from pairly_hire.messaging import recordings as message_recordings
from pairly_hire.messaging.drafting import (
    AI_TEMPLATES,
    MessageDraft,
    check_draft,
    draft_inputs,
    evidence_inputs,
)
from pairly_hire.pipeline.hooks import CriterionEvidence, ScreeningSummary
from pairly_hire.pipeline.stages import DEFAULT_STAGES, validate_stage_set

# ------------------------------------------------------------------ templates


def _source(t: DefaultTemplate) -> TemplateSource:
    return TemplateSource(
        t.channel,  # type: ignore[arg-type]
        t.locale,  # type: ignore[arg-type]
        t.subject,
        t.body,
        t.body_html,
        parse_declarations(t.variables),
    )


@pytest.mark.parametrize(
    "template", library.SYSTEM_TEMPLATES, ids=lambda t: f"{t.key}:{t.channel}:{t.locale}"
)
def test_every_template_lints_clean(template: DefaultTemplate) -> None:
    issues = lint_source(_source(template))
    assert [i for i in issues if i.severity == "error"] == []
    # Every declared variable is used (no warning that a value is never shown).
    assert [i.code for i in issues if i.code == "unused_variable"] == []
    for text in (template.subject, template.body):
        assert not has_em_dash(text)
    # Renders with its samples.
    rendered = render_source(_source(template), sample_variables(_source(template).variables))
    assert rendered.text
    assert not has_em_dash(rendered.text)


def test_every_template_has_english_and_arabic() -> None:
    seen: dict[tuple[str, str], set[str]] = defaultdict(set)
    for t in (*library.SYSTEM_TEMPLATES, *SENSITIVE):
        seen[(t.key, t.channel)].add(t.locale)
    for key, locales in seen.items():
        assert locales == {"en", "ar"}, key


def test_library_entries_are_defined_and_sensitive_ones_need_approval() -> None:
    for entry in library.LIBRARY:
        defs = library.definitions(entry.key)
        assert {d.locale for d in defs if d.channel == "email"} == {"en", "ar"}, entry.key
        assert all(d.requires_approval == entry.sensitive for d in defs), entry.key
    sensitive = {e.key for e in library.LIBRARY if e.sensitive}
    assert sensitive == {
        library.REJECTION,
        library.REJECTION_SCREENING,
        library.REJECTION_INTERVIEW,
        library.REJECTION_FINAL,
        library.OFFER,
    }
    # Rejections come in several tones and stages.
    assert len([e for e in library.LIBRARY if e.rejection]) >= 4


def test_ai_templates_declare_the_personal_paragraph_only_as_optional_text() -> None:
    for key in AI_TEMPLATES:
        for d in library.definitions(key):
            spec = d.variables["personal_note"]
            assert spec == {"type": "text", "required": False}
            # A model never fills the record's variables.
            assert set() == library.RECORD_VARIABLES & library.AI_VARIABLES


def test_rejection_tone_follows_the_stage() -> None:
    assert library.rejection_template_for("applied") == library.REJECTION_SCREENING
    assert library.rejection_template_for("screening") == library.REJECTION_SCREENING
    assert library.rejection_template_for("interview") == library.REJECTION_INTERVIEW
    assert library.rejection_template_for("offer") == library.REJECTION_FINAL
    assert library.rejection_template_for("custom_stage") == library.REJECTION_SCREENING


# ------------------------------------------------------------------ AI drafts


def test_message_recordings_are_current() -> None:
    assert message_recordings.main(["--check"]) == 0


def test_recorded_drafts_fill_only_the_paragraph_and_pass_the_fact_check() -> None:
    entries = parse_bundle(message_recordings.recordings_text())
    assert len(entries) == 3 * 2 * len(AI_TEMPLATES)
    for entry in entries:
        raw, inputs = entry["response"], entry["request"]["inputs"]
        draft = MessageDraft.model_validate(raw)
        assert set(raw) == {"personal_note", "reason"}
        assert draft.reason == ""
        assert check_draft(draft, inputs) == []
        # The model never sees the candidate: no name, no contact details.
        assert set(inputs) == {"template", "purpose", "job_title", "language", "evidence"}


@pytest.mark.parametrize(
    ("note", "problem"),
    [
        ("We can offer a 20 percent raise.", "the number 20"),
        ("Our office in Abu Dhabi would love to meet you.", "'Abu' is not in the record"),
        ("Call us on +971 50 123 4567 for details.", "a phone number"),
        ("Read more at https://example.test/jobs.", "a link"),
        ("Write to hr@example.test with questions.", "an email address"),
        ("Thank you—we enjoyed it.", "an em dash"),
        ("We prefer a younger team, so we chose someone aged under thirty.", "protected"),
        ("Your Senior Backend Engineer interview went to Sam Example.", "'Sam'"),
    ],
)
def test_drafts_that_add_facts_are_refused(note: str, problem: str) -> None:
    inputs = draft_inputs(library.STATUS_UPDATE, BACKEND.draft.title, "en")
    problems = check_draft(MessageDraft(personal_note=note), inputs)
    assert any(problem in p for p in problems), problems


def test_arabic_drafts_cannot_smuggle_latin_names_or_digits() -> None:
    inputs = draft_inputs(library.STATUS_UPDATE, BACKEND.draft.title_ar, "ar")
    assert check_draft(MessageDraft(personal_note="شكرا لصبرك، سنكتب لك قريبا."), inputs) == []
    problems = check_draft(MessageDraft(personal_note="سيتصل بك Ahmed خلال ٣ أيام."), inputs)
    assert any("'Ahmed'" in p for p in problems)
    assert any("number 3" in p for p in problems)


def test_reason_needs_rubric_evidence() -> None:
    inputs = draft_inputs(library.REJECTION_SCREENING, BACKEND.draft.title, "en")
    draft = MessageDraft(
        personal_note="Thank you for your interest in the Senior Backend Engineer role.",
        reason="The role needs production Python experience.",
    )
    assert "reason: given without rubric evidence" in check_draft(draft, inputs)
    summary = ScreeningSummary(
        status="scored",
        criteria=(
            CriterionEvidence("Production Python experience", "not_met", ("Built tools in Go",)),
        ),
    )
    with_evidence = draft_inputs(
        library.REJECTION_SCREENING, BACKEND.draft.title, "en", evidence_inputs(summary)
    )
    assert check_draft(draft, with_evidence) == []
    # Evidence never goes to a template that is not a rejection.
    assert (
        draft_inputs(library.STATUS_UPDATE, "x", "en", evidence_inputs(summary))["evidence"] == []
    )


# ------------------------------------------------------------------ stages


def test_default_stages() -> None:
    assert DEFAULT_STAGES.keys() == [
        "applied",
        "screening",
        "shortlist",
        "interview",
        "offer",
        "hired",
        "rejected",
        "withdrawn",
    ]
    assert DEFAULT_STAGES.next_after("applied").key == "screening"  # type: ignore[union-attr]
    assert DEFAULT_STAGES.next_after("offer").key == "hired"  # type: ignore[union-attr]
    assert DEFAULT_STAGES.next_after("hired") is None
    reveal = [s.key for s in DEFAULT_STAGES.stages if s.reveal_identity]
    assert reveal == ["shortlist", "interview", "offer", "hired"]


def _stages(*keys: str) -> list[dict[str, object]]:
    kinds = {"hired": "hired", "rejected": "rejected", "withdrawn": "withdrawn"}
    return [{"key": k, "label": k.title(), "kind": kinds.get(k, "active")} for k in keys]


def test_custom_stages_validate() -> None:
    custom = validate_stage_set(
        _stages("applied", "phone_screen", "case_study", "hired", "rejected", "withdrawn")
    )
    assert custom.next_after("phone_screen").key == "case_study"  # type: ignore[union-attr]
    for bad, message in (
        (_stages("screening", "applied", "hired", "rejected", "withdrawn"), "first stage"),
        (_stages("applied", "screening", "rejected", "withdrawn"), "'hired' stage"),
        (_stages("applied", "screening", "screening", "hired", "rejected", "withdrawn"), "unique"),
        (_stages("applied", "screening", "hired", "rejected", "offer"), "withdrawn"),
        ([{"key": "Applied!", "label": "x"}] * 4, "invalid stages"),
    ):
        with pytest.raises(BadRequestError, match=message):
            validate_stage_set(bad)


# ------------------------------------------------------------------ signed links

SECRET = "hire-links-secret-for-tests-" + "y" * 16


def test_preferences_token_round_trip_and_tamper() -> None:
    org, candidate = uuid.uuid4(), uuid.uuid4()
    token = links.prefs_token(SECRET, org, candidate)
    claim = links.read_prefs_token(SECRET, token)
    assert claim == links.PrefsClaim(org, candidate)
    assert links.read_prefs_token("another-secret-" + "z" * 20, token) is None
    other = token.replace(candidate.hex, uuid.uuid4().hex)
    assert links.read_prefs_token(SECRET, other) is None
    old = links.prefs_token(SECRET, org, candidate, now=time.time() - 200 * 24 * 3600)
    assert links.read_prefs_token(SECRET, old) is None
    # A referral token is not a preferences token, and the reverse.
    referral = links.referral_token(SECRET, org, candidate)
    assert links.read_prefs_token(SECRET, referral) is None
    assert links.read_referral_token(SECRET, token) is None
    assert links.read_referral_token(SECRET, referral) == links.ReferralClaim(org, candidate)
    assert links.read_referral_token(SECRET, referral[:-2] + "xx") is None
    for garbage in ("", "x" * 300, "rf1.a.b.c", "hp1...."):
        assert links.read_prefs_token(SECRET, garbage) is None
        assert links.read_referral_token(SECRET, garbage) is None


def test_preferences_url_keeps_the_token_in_the_fragment() -> None:
    config = links.LinkConfig(SECRET, "https://app.test.pairly")
    url = links.prefs_url(config, uuid.uuid4(), uuid.uuid4(), "ar")
    assert url.startswith("https://app.test.pairly/ar/candidate/preferences#hp1.")
    assert "?" not in url


# ------------------------------------------------------------------ plugin


def test_plugin_declares_graph_jobs_and_search_kind() -> None:
    plugin = discover_plugins()["hire"]
    graph = next(g for g in plugin.graphs if g.name == "hire_message")
    approval = graph.approval_type("hire.message")
    assert approval is not None
    assert approval.permission == "hire.message.approve"
    assert set(graph.final_actions) == {"send_candidate_message"}
    jobs = {j.name for j in plugin.scheduled_jobs}  # type: ignore[attr-defined]
    assert jobs == {
        "hire_screening_start",
        "hire_sla_drafts",
        "hire_rediscovery_index",
        "hire_interview_reminders",
        "hire_scorecard_reminders",
        "hire_erasure_objects",
    }
    kinds = {s.kind.key for s in plugin.search_sources}  # type: ignore[attr-defined]
    assert kinds == {"hire_candidate"}
    registry = build_registry([plugin])
    # Approving a message needs a second factor; employees and agents never approve.
    assert "hire.message.approve" not in registry.permissions_for_role("employee")
    assert "hire.message.approve" not in registry.permissions_for_role("agent")
    assert "hire.candidate.decide" not in registry.permissions_for_role("interviewer")


def test_recordings_json_is_valid_json() -> None:
    data = json.loads(message_recordings.recordings_text())
    assert data["entries"]
