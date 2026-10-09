# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Screening without a database: blind redaction, no evidence no credit, the overall
score, knockouts and claim checks, on the synthetic Hire data (``synth.hire``).

The CVs are rendered and parsed in process with the sandbox's parsers. Model answers
come from ``synth.hire.screening`` (scripted from the synthetic ground truth, never
model output); what is tested here is the deterministic code around them.
"""

from __future__ import annotations

import asyncio
import json
import random
import re
from functools import cache
from pathlib import Path
from typing import Any

import pytest

from pairly_core.extraction.evidence import normalize
from pairly_hire.rubric import DEFAULT_ANCHORS, SCALE_MAX, Criterion, Rubric, parse_rubric
from pairly_hire.scoring.aggregate import ScoreRangeError, breakdown, overall_score
from pairly_hire.scoring.credit import credit
from pairly_hire.scoring.model import CvScoreOutput
from pairly_hire.screening import pipeline
from pairly_hire.screening.blind import (
    PLACEHOLDER,
    BlindConfig,
    BlindedInput,
    BlindSources,
    display_text,
    redact_text,
)
from pairly_hire.screening.claims import ClaimConfig, date_checks
from pairly_hire.screening.knockouts import SUGGESTED_ACTIONS, check_knockouts
from pairly_hire.screening.profile import (
    Job,
    Located,
    ParsedProfile,
    normalize_year_month,
)
from synth.hire.dataset import DocSpec, build
from synth.hire.jobs import load_jobs
from synth.hire.screening import (
    SCREENING_AS_OF,
    contact_for,
    job_by_id,
    parse_spec,
    screen,
    screening_rubric,
)

LANGUAGE_LEVEL = re.compile(r"\((?:Native|Fluent|Professional|Conversational|Basic)\)")
MANIFESTS = Path(__file__).resolve().parents[3] / "synth" / "hire" / "manifests"


@cache
def specs() -> dict[str, DocSpec]:
    return {s.record.id: s for s in build(300, 2026)}


@cache
def parsed(doc_id: str) -> Any:
    return parse_spec(specs()[doc_id])


def blinded_for(doc_id: str, config: BlindConfig | None = None) -> BlindedInput:
    spec = specs()[doc_id]
    doc = parsed(doc_id)
    guarded = pipeline.guard(doc, pipeline.scan(doc, Path(spec.record.file).name))
    return pipeline.blind_input(guarded, contact_for(spec), None, config or BlindConfig())


def _words(text: str) -> set[str]:
    return set(re.findall(r"\w+", text.casefold()))


def _markers(spec: DocSpec) -> list[str]:
    """Every protected marker the fairness pairs vary or show."""
    gt = spec.record.ground_truth.contact
    persona = spec.record.persona
    markers = [gt.name, gt.email, gt.phone, f"{gt.city}, {gt.country}"]
    markers += [link.url for link in gt.links]
    markers += [p for p in re.split(r"\s+", gt.name) if len(p) >= 3]
    if persona.personal_details_shown:
        markers += [str(persona.birth_year), persona.gender.capitalize()]
    markers += [e.institution for e in spec.record.ground_truth.education]
    return markers


def _fairness_ids() -> list[str]:
    return sorted(i for i in specs() if i.startswith("fair-"))


# ------------------------------------------------------------------ blind redaction


@pytest.mark.parametrize("doc_id", _fairness_ids())
def test_blind_redaction_removes_every_protected_marker(doc_id: str) -> None:
    spec = specs()[doc_id]
    blinded = blinded_for(doc_id)
    folded = normalize(blinded.text).text
    words = _words(blinded.text)
    for marker in _markers(spec):
        needle = normalize(marker).text
        if " " in needle or "@" in needle or "." in needle or "+" in needle:
            assert needle not in folded, (doc_id, marker)
        else:
            assert needle.casefold() not in words, (doc_id, marker)
    # The nationality: gone everywhere but where the same word names a language the
    # person speaks ("Spanish (Native)"), which is a skill.
    if spec.record.persona.personal_details_shown:
        outside_languages = " ".join(
            line for line in blinded.text.splitlines() if not LANGUAGE_LEVEL.search(line)
        )
        assert spec.record.persona.nationality.casefold() not in _words(outside_languages)
    # The gender words a reviewer could read the gender from are gone too ("ms" stays:
    # it is milliseconds in these CVs; the title "Ms" is checked case-sensitively).
    assert not words & {"female", "male", "mr", "mrs", "miss"}
    assert not re.search(r"\b(?:Mr|Mrs|Ms|Miss)\b", blinded.text)


def test_blinded_input_is_identical_across_every_fairness_pair() -> None:
    """Apart from where a PDF wraps a line (a longer name or email moves a line break),
    the scoring model reads the same text for both CVs of every pair."""
    pairs = json.loads((MANIFESTS / "fairness_pairs.json").read_text(encoding="utf-8"))
    for pair in pairs:
        base = blinded_for(pair["base"])
        variant = blinded_for(pair["variant"])
        assert base.text.split() == variant.text.split(), pair


@pytest.mark.parametrize("doc_id", ["fair-01-base", "fair-02-base", "cv-0026", "cv-0029"])
def test_offsets_map_back_to_the_original_cv(doc_id: str) -> None:
    doc = parsed(doc_id)
    blinded = blinded_for(doc_id)
    rng = random.Random(doc_id)  # noqa: S311  (test sampling, not security)
    checked = 0
    for _ in range(300):
        start = rng.randrange(0, max(1, len(blinded.text) - 20))
        end = start + rng.randrange(3, 20)
        mapped = blinded.to_original(start, end)
        piece = blinded.text[start:end]
        if any(p in piece for p in PLACEHOLDER.values()) or "[" in piece or "]" in piece:
            continue
        if mapped is None:
            continue
        checked += 1
        original = doc.text[mapped[0] : mapped[1]]
        assert normalize(original).text == normalize(piece).text
    assert checked > 50
    # A range that touches a placeholder maps to nothing.
    hidden = blinded.text.index("[name]")
    assert blinded.to_original(hidden, hidden + 6) is None
    assert blinded.to_original(max(0, hidden - 3), hidden + 2) is None
    # The reviewer's view maps evidence offsets the same way.
    view = display_text(doc.text, blinded.original_spans())
    assert "[name]" in view.text
    name = specs()[doc_id].record.ground_truth.contact.name
    assert name not in view.text


def test_universities_are_configurable_and_blind_mode_can_be_off() -> None:
    institution = specs()["fair-01-base"].record.ground_truth.education[0].institution
    on = blinded_for("fair-01-base")
    assert institution not in on.text
    kept = blinded_for("fair-01-base", BlindConfig(universities=False))
    assert institution in kept.text
    off = blinded_for("fair-01-base", BlindConfig(enabled=False))
    assert off.spans == ()
    assert off.text == off.guarded.text
    sources = BlindSources(names=("Layla Haddad",), emails=("layla@example.test",))
    redacted = redact_text("Author: Layla Haddad <layla@example.test>", sources, BlindConfig())
    assert "Layla" not in redacted
    assert "layla@" not in redacted


# ------------------------------------------------------------------ no evidence, no credit


def _output(scores: dict[str, tuple[int, list[str]]]) -> CvScoreOutput:
    return CvScoreOutput.model_validate(
        {
            "criteria": [
                {
                    "criterion_id": cid,
                    "score": score,
                    "confidence": 0.9,
                    "evidence": quotes,
                    "why_not_higher": "",
                }
                for cid, (score, quotes) in scores.items()
            ],
            "confidence": 0.9,
        }
    )


def test_no_evidence_no_credit_for_hidden_redacted_and_invented_quotes() -> None:
    doc_id = "atk-white-text-skills-pdf"
    spec = specs()[doc_id]
    attack = spec.record.attack
    assert attack is not None
    doc = parsed(doc_id)
    blinded = blinded_for(doc_id)
    rubric = screening_rubric(job_by_id("backend_engineer"))
    visible = spec.text.by_ref("exp:0:h:0")[0].text
    hidden = next(p for p in attack.payloads if p)
    assert hidden in doc.text  # in the file, removed from what the model reads
    name = spec.record.ground_truth.contact.name
    ids = [c.id for c in rubric.criteria]
    result = credit(
        _output(
            {
                ids[0]: (3, [visible]),
                ids[1]: (4, [hidden]),
                ids[2]: (2, [name]),
                ids[3]: (2, ["Ran Kubernetes for a bank of 10,000 people"]),
                ids[4]: (1, [visible, "invented text that is nowhere"]),
            }
        ),
        rubric,
        doc,
        blinded,
    )
    by_id = {c.criterion_id: c for c in result.criteria}
    assert by_id[ids[0]].score == 3
    assert by_id[ids[0]].evidence[0].quote.split() == visible.split()
    start, end = by_id[ids[0]].evidence[0].start, by_id[ids[0]].evidence[0].end
    assert normalize(doc.text[start:end]).text == normalize(visible).text
    for cid in ids[1:4]:
        assert by_id[cid].score == 0, cid
        assert "no_evidence" in by_id[cid].flags
        assert by_id[cid].rejected
    assert by_id[ids[4]].score == 1
    assert len(by_id[ids[4]].rejected) == 1
    # Criteria the model did not answer count 0 and need a person.
    assert by_id[ids[5]].flags == ("not_scored",)
    assert f"score_missing:{ids[5]}" in result.reasons
    assert result.overall == overall_score(rubric, result.scores)


def test_why_not_higher_is_plain_text_without_protected_attributes() -> None:
    doc_id = "cv-0026"
    rubric = screening_rubric(job_by_id("backend_engineer"))
    cid = rubric.criteria[0].id
    for text, kept in (
        ("Needs five years of backend work.", True),
        ("Too old for the team.", True),  # no banned word: kept as text
        ("She is female, so lower.", False),
        ("See <script>alert(1)</script>", False),
        ("Read https://example.test/cv", False),
    ):
        output = CvScoreOutput.model_validate(
            {
                "criteria": [
                    {
                        "criterion_id": cid,
                        "score": 0,
                        "confidence": 0.9,
                        "evidence": [],
                        "why_not_higher": text,
                    }
                ],
                "confidence": 0.9,
            }
        )
        result = credit(output, rubric, parsed(doc_id), blinded_for(doc_id))
        assert bool(result.criteria[0].why_not_higher) is kept, text


# ------------------------------------------------------------------ the overall score


def test_aggregation_is_the_synthetic_label_formula() -> None:
    jobs = {j.id: j for j in load_jobs()}
    checked = 0
    for name in ("cvs.jsonl", "attacks.jsonl", "fairness.jsonl"):
        for line in (MANIFESTS / name).read_text(encoding="utf-8").splitlines():
            record = json.loads(line)
            for labels in record["labels"]:
                rubric = jobs[labels["job_id"]].rubric
                scores = {c["criterion_id"]: c["score"] for c in labels["criteria"]}
                weights = {c.id: c.weight for c in rubric.criteria}
                independent = round(
                    100
                    * sum(weights[k] * v for k, v in scores.items())
                    / (sum(weights.values()) * SCALE_MAX),
                    2,
                )
                assert overall_score(rubric, scores) == independent == labels["overall"]
                checked += 1
    assert checked > 900
    rubric = jobs["backend_engineer"].rubric
    total, shares = breakdown(rubric, {})
    assert total == 0.0
    assert all(s.points == 0 for s in shares)
    with pytest.raises(ScoreRangeError):
        overall_score(rubric, {rubric.criteria[0].id: 5})
    # Identical criterion scores give identical overall scores, whatever else differs.
    same = {c.id: 2 for c in rubric.criteria}
    assert overall_score(rubric, same) == overall_score(rubric, dict(reversed(same.items())))


def test_rubric_scale_and_anchors() -> None:
    criterion = Criterion(id="x", name="X", description="d", evidence="e", weight=2)
    assert criterion.scale_anchors == DEFAULT_ANCHORS
    assert len(DEFAULT_ANCHORS) == SCALE_MAX + 1
    with pytest.raises(ValueError, match="anchors"):
        Criterion(id="x", name="X", description="d", evidence="e", weight=2, anchors=("a" * 5,))
    rubric = screening_rubric(job_by_id("backend_engineer"))
    # A rubric with anchors locks and parses back exactly.
    assert parse_rubric(rubric.to_yaml()) == rubric
    plain = Rubric.model_validate(job_by_id("ap_accountant").rubric.model_dump())
    assert "anchors" not in plain.to_yaml()


# ------------------------------------------------------------------ knockouts


def test_knockouts_never_reject() -> None:
    assert not any("reject" in a for a in SUGGESTED_ACTIONS)
    jobs = {j.id: j for j in load_jobs()}
    for spec in specs().values():
        for labels in spec.record.labels:
            checks = check_knockouts(jobs[labels.job_id].rubric, labels.knockout_answers)
            routes = {c.as_dict()["route"] for c in checks}
            assert routes <= {"pass", "human_review"}
            expected = "pass" if routes <= {"pass"} else "human_review"
            assert expected == labels.knockout_route
            for c in checks:
                if not c.matches:
                    assert c.suggested_action in SUGGESTED_ACTIONS
                    assert c.suggestion
    unanswered = check_knockouts(jobs["backend_engineer"].rubric, {})
    assert all(c.suggested_action == "ask_candidate" for c in unanswered)


# ------------------------------------------------------------------ claim checks


@pytest.mark.parametrize(
    "doc_id",
    sorted(i for i, s in specs().items() if s.record.attack and s.record.attack.claim_flags),
)
def test_claim_checks_catch_the_synthetic_claim_attacks(doc_id: str) -> None:
    spec = specs()[doc_id]
    attack = spec.record.attack
    assert attack is not None
    case = asyncio.run(screen(spec, job_by_id("backend_engineer"), doc=parsed(doc_id)))
    codes = {f.code for f in case.flags}
    assert set(attack.claim_flags) <= codes, (doc_id, codes)
    assert all(f.as_dict()["severity"] == "info" for f in case.flags)
    # Never a penalty: the score is the label's, whatever the flags say.
    label = next(lab for lab in spec.record.labels if lab.job_id == "backend_engineer")
    assert case.scoring is not None
    assert case.scoring.overall == label.overall


def test_year_only_dates_are_never_flagged_on_their_own() -> None:
    def job(start: str, end: str | None) -> Job:
        return Job(Located("Acme LLC"), Located("Engineer"), start, end)

    adjacent = ParsedProfile(
        experience=(job("2024", None), job("2022", "2023"), job("2021", "2022"))
    )
    assert date_checks(adjacent, SCREENING_AS_OF, ClaimConfig()) == []
    overlapping = ParsedProfile(experience=(job("2024-01", None), job("2022-01", "2025-06")))
    assert {f.code for f in date_checks(overlapping, SCREENING_AS_OF, ClaimConfig())} == {
        "overlapping_roles"
    }
    assert normalize_year_month("Mar 2019 to Present") == "2019-03"
    assert normalize_year_month("مارس 2019 - حتى الآن") == "2019-03"
    assert normalize_year_month("03/2019 - 06/2021") == "2019-03 2021-06"
