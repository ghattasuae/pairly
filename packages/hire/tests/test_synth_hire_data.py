# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""The synthetic Hire dataset (WP2.1): rubrics, determinism, labels, renders, manifests."""

from __future__ import annotations

import copy
import json
import re
from collections import Counter
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

import pairly_hire
from pairly_core.documents.docx import DocxParser
from pairly_core.documents.ocr import ImageParser
from pairly_core.documents.pdf import PdfParser
from pairly_core.documents.registry import ParseOptions
from pairly_core.guardrails import quote_exists
from pairly_hire.rubric import Rubric, rubric_validator
from synth.hire.__main__ import main as cli
from synth.hire.dataset import DocSpec, build
from synth.hire.facts import make_facts, transliterate
from synth.hire.jobs import JOBS_DIR, load_jobs
from synth.hire.labels import KNOCKOUT_ANSWERS, label_all, overall_score, rule_table
from synth.hire.models import Job
from synth.hire.text import build_text

OPTIONS = ParseOptions(ocr_languages="eng")


@pytest.fixture(scope="module")
def specs() -> list[DocSpec]:
    return build(40, 2026)


def test_jobs_and_rubrics_load() -> None:
    jobs = load_jobs()
    assert [j.id for j in jobs] == ["backend_engineer", "ap_accountant", "recruiter"]
    lock = rubric_validator()
    for job in jobs:
        rubric = job.rubric
        # The sample rubrics are canonical Hire rubrics: what a recruiter would lock.
        assert isinstance(rubric, Rubric)
        assert lock(rubric.to_yaml()) == rubric
        assert rubric.must_haves
        assert overall_score(rubric, {c.id: 4 for c in rubric.criteria}) == 100.0
        assert overall_score(rubric, {}) == 0.0
        assert set(rule_table()[job.id]) == {c.id for c in rubric.criteria}
        assert set(KNOCKOUT_ANSWERS) >= {k.id for k in rubric.knockout_questions}


def test_job_rules_are_enforced() -> None:
    data = yaml.safe_load((JOBS_DIR / "backend_engineer.yaml").read_text(encoding="utf-8"))
    Job.model_validate(data)
    guide = data["label_guide"]
    first = next(iter(guide))
    with pytest.raises(ValidationError, match="anchors"):
        Job.model_validate({**data, "label_guide": {**guide, first: {"anchors": ["Only."]}}})
    with pytest.raises(ValidationError, match="label_guide"):
        Job.model_validate({**data, "label_guide": {k: v for k, v in guide.items() if k != first}})
    with pytest.raises(ValidationError, match="job_title"):
        Job.model_validate({**data, "rubric": {**data["rubric"], "job_title": "Other"}})
    # The rubric rules are the plugin's own: weights 1 to 10, yes or no knockouts.
    heavy = [{**c, "weight": 30} for c in data["rubric"]["must_haves"]]
    with pytest.raises(ValidationError, match="weight"):
        Job.model_validate({**data, "rubric": {**data["rubric"], "must_haves": heavy}})


def test_hire_plugin_never_imports_synth() -> None:
    plugin = Path(pairly_hire.__file__).resolve().parent
    offenders = [
        str(path.relative_to(plugin))
        for path in plugin.rglob("*.py")
        if re.search(r"^\s*(from|import)\s+synth\b", path.read_text(encoding="utf-8"), re.M)
    ]
    assert offenders == []


def test_build_is_deterministic(specs: list[DocSpec]) -> None:
    again = build(40, 2026)
    assert [s.record for s in specs] == [s.record for s in again]
    other = build(40, 7)
    assert [s.record for s in specs] != [s.record for s in other]
    kinds = Counter(s.record.kind for s in specs)
    assert kinds["cv"] == 40
    assert kinds["attack"] >= 40
    assert kinds["fairness"] == 72


def test_every_label_quotes_text_on_the_cv(specs: list[DocSpec]) -> None:
    for spec in specs:
        lines = spec.text.plain
        for labels in spec.record.labels:
            for crit in labels.criteria:
                assert (crit.score == 0) == (not crit.evidence), (spec.record.id, crit)
                for quote in crit.evidence:
                    assert quote in lines, (spec.record.id, crit.criterion_id, quote)


def test_labels_never_read_the_persona() -> None:
    facts = make_facts("cv-persona", 2026, family="accounts_payable", level="senior")
    jobs = list(load_jobs())
    text = build_text(facts, "en", "mon_year")
    before = label_all(jobs, facts, text)
    changed = copy.deepcopy(facts)
    changed.person.gender = "male" if facts.person.gender == "female" else "female"
    changed.person.nationality = "Testland"
    changed.person.birth_year -= 30
    changed.person.photo = not facts.person.photo
    assert label_all(jobs, changed, build_text(changed, "en", "mon_year")) == before


def test_fairness_pairs_differ_in_one_attribute(specs: list[DocSpec]) -> None:
    by_id = {s.record.id: s for s in specs}
    variants = [s for s in specs if s.record.fairness and s.record.fairness.role == "variant"]
    assert Counter(s.record.fairness.dimension for s in variants if s.record.fairness) == {
        "name": 12,
        "gender": 12,
        "nationality": 12,
        "age": 12,
        "photo": 12,
    }
    for spec in variants:
        info = spec.record.fairness
        assert info is not None
        base = by_id[f"{info.pair_id}-base"]
        assert spec.record.labels == base.record.labels
        diff = {
            (a.text, b.text)
            for a, b in zip(base.text.lines, spec.text.lines, strict=True)
            if a.text != b.text
        }
        if info.dimension == "photo":
            assert not diff
            assert spec.record.persona.photo
            assert not base.record.persona.photo
        elif info.dimension == "nationality":
            assert {a.split(":")[0] for a, _ in diff} == {"Nationality"}
        elif info.dimension == "age":
            assert {a.split(":")[0] for a, _ in diff} == {"Year of birth"}
        else:
            assert diff  # the name (and the email built from it), or the gender line
            assert base.record.ground_truth.experience == spec.record.ground_truth.experience


def test_attack_manifest_says_what_to_flag(specs: list[DocSpec]) -> None:
    attacks = [s.record for s in specs if s.record.kind == "attack"]
    techniques = {t for r in attacks if r.attack for t in r.attack.techniques}
    for needed in (
        "white_text",
        "near_white_text",
        "tiny_font",
        "render_mode_3",
        "text_under_shape",
        "clipped_text",
        "off_page",
        "instruction",
        "paraphrased_instruction",
        "keyword_stuffing",
        "metadata_injection",
        "bidi_override",
        "zero_width",
        "inconsistent_dates",
        "overlapping_dates",
        "title_inflation",
        "unsupported_skills",
    ):
        assert needed in techniques, needed
    for record in attacks:
        info = record.attack
        assert info is not None
        if info.caught_by == "claim_consistency":
            assert not info.expected_findings
            assert info.claim_flags
        else:
            assert info.expected_findings
    langs = Counter(r.attack.language for r in attacks if r.attack)
    assert langs["ar"] >= 5


def test_transliteration_is_latin() -> None:
    assert transliterate("محمد الجابر").isascii()
    assert transliterate("").strip() == ""


def _parse(spec: DocSpec, tmp: Path) -> str:
    path = tmp / Path(spec.record.file).name
    path.write_bytes(spec.render())
    if spec.record.format == "docx":
        return DocxParser().parse(path, OPTIONS).text
    if spec.record.format in ("png", "jpg"):
        return ImageParser().parse(path, OPTIONS).text
    return PdfParser().parse(path, OPTIONS).text


def test_every_format_and_layout_renders(specs: list[DocSpec], tmp_path: Path) -> None:
    seen: dict[tuple[str, str], DocSpec] = {}
    for spec in specs:
        seen.setdefault((spec.record.format, spec.record.layout), spec)
    assert {"pdf", "docx"} <= {f for f, _ in seen}
    assert {"png", "jpg"} & {f for f, _ in seen}
    for (fmt, layout), spec in sorted(seen.items()):
        data = spec.render()
        assert data, (fmt, layout)
        if fmt in ("png", "jpg"):
            continue  # OCR is measured by the evals, not here
        text = _parse(spec, tmp_path)
        assert text.strip(), (fmt, layout)
        if spec.record.language == "en" and layout in ("single_column", "table"):
            for labels in spec.record.labels:
                for crit in labels.criteria:
                    for quote in crit.evidence:
                        assert quote_exists(text, quote), (spec.record.id, quote)


def test_render_is_deterministic(specs: list[DocSpec]) -> None:
    for spec in specs[:6]:
        assert spec.render() == spec.render()


def test_cli_generates_and_checks(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli(["generate", "--out", str(tmp_path), "--count", "3"]) == 0
    index = json.loads((tmp_path / "index.json").read_text(encoding="utf-8"))
    assert index["count"] == 3
    for doc in index["documents"]:
        assert (tmp_path / doc["file"]).is_file()
    capsys.readouterr()
    assert cli(["--check"]) == 0
    assert "match the generators" in capsys.readouterr().out
