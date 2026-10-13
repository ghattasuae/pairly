# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""The Hire screening evals (WP2.10). See ``evals/hire/__init__.py`` for what is measured.

Every CV of the synthetic dataset (PDF and DOCX; scans need OCR and are skipped unless
``--include-scans``) goes through ``pairly_hire.screening.pipeline`` exactly as the
graph runs it, with the model steps answered by ``synth.hire.screening.ScriptedRouter``
(scripted from the synthetic ground truth). The answers are then recorded as a bundle
and the whole run is replayed through ``BundleRouter`` (request hashes as the model
router keys them), so the measured pass also proves that the pipeline is deterministic.

No number here is invented: deterministic metrics are measured on this run; anything
that depends on what a real model answers is ``pending_live_run`` until a run with a
model key exists.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import statistics
import time
import uuid
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from pairly_core.extraction.evidence import normalize
from pairly_hire.screening import counterfactual, pipeline
from pairly_hire.screening.blind import BlindConfig, BlindSources
from pairly_hire.screening.knockouts import check_knockouts
from synth.hire.dataset import DEFAULT_COUNT, DEFAULT_SEED, DocSpec, build
from synth.hire.models import Job
from synth.hire.screening import (
    DEMO_APPLICATIONS,
    DEMO_JOB,
    SCRIPTED_NOTE,
    BundleRouter,
    Case,
    contact_for,
    demo_extras,
    job_by_id,
    parse_spec,
    recordings_text,
    screen,
    upload_name,
)

RESULTS_DIR: Final = Path(__file__).resolve().parents[1] / "results"
RESULTS: Final = RESULTS_DIR / "hire.json"
GUARDRAILS: Final = RESULTS_DIR / "guardrails_hire.json"
REPORT: Final = Path(__file__).resolve().parents[1] / "report.md"
# An organisation's fairness run summaries, exported from the bias report (optional).
ORG_FAIRNESS_RUNS: Final = RESULTS_DIR / "fairness_runs.json"
SCANS: Final = frozenset({"png", "jpg", "scan_pdf"})
PENDING: Final = "pending_live_run"
LANGUAGE_LEVEL: Final = re.compile(r"\((?:Native|Fluent|Professional|Conversational|Basic)\)")


def pending(reason: str) -> dict[str, str]:
    return {"status": PENDING, "reason": reason}


def ratio(num: int, den: int) -> float | None:
    return round(num / den, 4) if den else None


FAMILY_JOB: Final = {
    "backend": "backend_engineer",
    "frontend": "backend_engineer",
    "data_analyst": "backend_engineer",
    "accounts_payable": "ap_accountant",
    "accountant": "ap_accountant",
    "recruiting": "recruiter",
    "hr_generalist": "recruiter",
    "customer_support": "recruiter",
}


def job_for(spec: DocSpec) -> Job:
    """Each CV is scored against the sample job closest to its family (labels exist
    for every CV and every sample job)."""
    return job_by_id(FAMILY_JOB.get(spec.record.family, "backend_engineer"))


# ------------------------------------------------------------------ markers


def _word_present(text: str, word: str) -> bool:
    return bool(re.search(rf"(?<!\w){re.escape(word.casefold())}(?!\w)", text.casefold()))


def markers(spec: DocSpec, *, universities: bool = True) -> list[tuple[str, str]]:
    """(category, marker) pairs a blind CV must not show."""
    gt = spec.record.ground_truth.contact
    persona = spec.record.persona
    out: list[tuple[str, str]] = [("name", gt.name), ("contact", gt.email), ("contact", gt.phone)]
    if gt.name_alt:
        out.append(("name", gt.name_alt))
    out += [("name", p) for p in re.split(r"\s+", gt.name) if len(p) >= 3]
    out += [("contact", link.url) for link in gt.links]
    out.append(("address", f"{gt.city}, {gt.country}"))
    if persona.personal_details_shown:
        out += [
            ("nationality", persona.nationality),
            ("age", str(persona.birth_year)),
            ("gender", persona.gender.capitalize()),
        ]
    if universities:
        out += [("university", e.institution) for e in spec.record.ground_truth.education]
    return out


def marker_shown(category: str, marker: str, text: str) -> bool:
    if category == "nationality":
        # The same word as a spoken language ("Spanish (Native)") is a skill.
        text = "\n".join(line for line in text.splitlines() if not LANGUAGE_LEVEL.search(line))
    folded = normalize(text).text
    needle = normalize(marker).text
    if not needle:
        return False
    if re.fullmatch(r"\w+", needle):
        return _word_present(folded, needle)
    return needle in folded


# ------------------------------------------------------------------ the run


@dataclass
class Collected:
    cases: list[Case] = field(default_factory=list)
    entries: list[dict[str, Any]] = field(default_factory=list)
    replay_mismatches: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


async def _run_case(spec: DocSpec, job: Job, collected: Collected) -> None:
    clock = time.monotonic()
    doc = parse_spec(spec)
    parse_seconds = time.monotonic() - clock
    scripted = await screen(spec, job, doc=doc)
    collected.entries += scripted.entries
    # The measured pass: the same pipeline, every model answer replayed by request hash.
    replay = BundleRouter.of(scripted.entries)
    case = await screen(spec, job, router=replay, doc=doc)
    if (case.scoring is None) != (scripted.scoring is None) or (
        case.scoring is not None
        and scripted.scoring is not None
        and case.scoring.scores != scripted.scoring.scores
    ):
        collected.replay_mismatches.append(spec.record.id)
    if (
        case.blinded is None
        or scripted.blinded is None
        or case.blinded.text != scripted.blinded.text
    ):
        collected.replay_mismatches.append(spec.record.id)
    case.timings["parse"] = parse_seconds
    collected.cases.append(case)


def collect(specs: Iterable[DocSpec]) -> Collected:
    collected = Collected()
    for spec in specs:
        try:
            asyncio.run(_run_case(spec, job_for(spec), collected))
        except Exception as exc:  # recorded, never hidden: the gate fails on errors
            collected.errors.append(f"{spec.record.id}: {type(exc).__name__}")
    return collected


# ------------------------------------------------------------------ metrics


def extraction_metrics(cases: list[Case]) -> dict[str, Any]:
    """Per field: the share of ground-truth values the extraction framework accepts
    (evidence found in the parsed CV, value parsed and backed by the quote) when the
    answer is the ground truth itself. Parsing, evidence location and value checks are
    real; how often a real model gives that answer is pending."""
    accepted: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    by_layer: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for case in cases:
        if case.profile is None:
            continue
        values = case.profile.as_dict()
        gt = case.spec.record.ground_truth

        def tally(key: str, ok: bool, layer: str = case.spec.record.text_layer) -> None:
            accepted[key][1] += 1
            accepted[key][0] += int(ok)
            by_layer[layer][1] += 1
            by_layer[layer][0] += int(ok)

        contact = values["contact"]
        for name, expected in (
            ("name", gt.contact.name),
            ("email", gt.contact.email),
            ("phone", gt.contact.phone),
            ("city", gt.contact.city),
            ("country", gt.contact.country),
            ("other_name", gt.contact.name_alt),
        ):
            if expected is None:
                continue
            got = contact.get(name)
            tally(f"contact.{name}", bool(got) and got["value"] == expected)
        jobs = values["experience"]
        for i, job in enumerate(gt.experience):
            got_job = jobs[i] if i < len(jobs) else {}
            employer = (got_job.get("employer") or {}).get("value")
            title = (got_job.get("title") or {}).get("value")
            tally("experience.employer", employer == job.employer)
            tally("experience.title", title == job.title)
            tally("experience.start", got_job.get("start") == job.start)
            if job.end is not None:
                tally("experience.end", got_job.get("end") == job.end)
        edus = values["education"]
        for i, edu in enumerate(gt.education):
            got_edu = edus[i] if i < len(edus) else {}
            tally("education.institution", got_edu.get("institution") == edu.institution)
            tally("education.degree", got_edu.get("degree") == edu.degree)
            tally("education.field", got_edu.get("field") == edu.field)
            tally("education.end_year", got_edu.get("end_year") == str(edu.end_year))
        skills = {s["value"] for s in values["skills"]}
        for skill in gt.skills:
            tally("skills", skill in skills)
        certs = {c.get("name") for c in values["certifications"]}
        for cert in gt.certifications:
            tally("certifications", cert.name in certs)
        langs = {(x.get("language"), x.get("level")) for x in values["languages"]}
        for lang in gt.languages:
            tally("languages", (lang.language, lang.level) in langs)
        links = {x["value"] for x in values["links"]}
        for link in gt.contact.links:
            tally("links", link.url in links)
    total = [sum(v[0] for v in accepted.values()), sum(v[1] for v in accepted.values())]
    return {
        "what": (
            "Share of ground-truth values the extraction framework accepts (evidence "
            "located in the parsed CV, value parsed and backed by its quote) when the "
            "model's answer is the ground truth: the ceiling set by parsing, evidence "
            "location and value checks. Not a model accuracy."
        ),
        "deterministic_ceiling": {
            "rate": ratio(*total),
            "values": total[1],
            "per_field": {
                k: {"values": n, "accepted": hit, "rate": ratio(hit, n)}
                for k, (hit, n) in sorted(accepted.items())
            },
            "per_text_layer": {
                k: {"values": n, "accepted": hit, "rate": ratio(hit, n)}
                for k, (hit, n) in sorted(by_layer.items())
            },
        },
        "model_accuracy": pending("needs model output; scripted recordings echo the ground truth"),
    }


def evidence_metrics(cases: list[Case]) -> dict[str, Any]:
    """Every accepted evidence quote of every scoring result: it exists verbatim
    (normalised) at its location in the original CV, outside blind-redacted and
    guardrail-removed text."""
    total = valid = 0
    rejected: Counter[str] = Counter()
    problems: list[str] = []
    for case in cases:
        if case.scoring is None or case.blinded is None:
            continue
        text = case.doc.text
        hidden = [(s, e) for s, e, _ in case.blinded.original_spans()]
        hidden += [(s.start, s.end) for s in case.guarded.model_input.removed]
        for criterion in case.scoring.criteria:
            rejected.update(r.reason for r in criterion.rejected)
            for evidence in criterion.evidence:
                total += 1
                at = text[evidence.start : evidence.end]
                ok = (
                    normalize(at).text == normalize(evidence.quote).text
                    and normalize(evidence.quote).text in normalize(text).text
                    and not any(s < evidence.end and e > evidence.start for s, e in hidden)
                )
                valid += int(ok)
                if not ok and len(problems) < 20:
                    problems.append(f"{case.spec.record.id}:{criterion.criterion_id}")
    return {
        "rate": ratio(valid, total),
        "quotes": total,
        "valid": valid,
        "invalid_examples": problems,
        "rejected_quotes": dict(rejected),
        "measured_on": "recorded (scripted) scoring results; live results: none yet",
    }


def credit_metrics(cases: list[Case]) -> dict[str, Any]:
    """With perfect scripted answers, how often credit is lost to evidence that cannot
    be quoted from the blinded text (information, not agreement)."""
    criteria = lost = 0
    deltas: list[float] = []
    for case in cases:
        if case.scoring is None:
            continue
        labels = next(lab for lab in case.spec.record.labels if lab.job_id == job_for(case.spec).id)
        expected = {c.criterion_id: c.score for c in labels.criteria}
        for c in case.scoring.criteria:
            criteria += 1
            lost += int(c.score < expected.get(c.criterion_id, 0))
        deltas.append(round(labels.overall - case.scoring.overall, 2))
    return {
        "what": (
            "Scripted answers carry the synthetic label and its evidence lines. Credit "
            "is lost where a line cannot be quoted from the blinded text. This measures "
            "the pipeline, not a model."
        ),
        "criteria": criteria,
        "criteria_with_lost_credit": lost,
        "share_with_lost_credit": ratio(lost, criteria),
        "overall_points_lost": {
            "mean": round(statistics.mean(deltas), 3) if deltas else None,
            "max": max(deltas) if deltas else None,
        },
    }


def _blind_rate(cases: list[Case], *, with_profile: bool) -> dict[str, Any]:
    present = removed = 0
    by_category: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    leaks: list[str] = []
    for case in cases:
        before = case.guarded.text
        if with_profile:
            blinded = case.blinded
        else:
            blinded = pipeline.blind_input(
                case.guarded, contact_for(case.spec), None, BlindConfig()
            )
        if blinded is None:
            continue
        for category, marker in markers(case.spec):
            if not marker_shown(category, marker, before):
                continue  # not in the text the model could read anyway
            present += 1
            by_category[category][1] += 1
            gone = not marker_shown(category, marker, blinded.text)
            removed += int(gone)
            by_category[category][0] += int(gone)
            if not gone and len(leaks) < 30:
                leaks.append(f"{case.spec.record.id}:{category}")
    return {
        "rate": ratio(removed, present),
        "markers": present,
        "removed": removed,
        "per_category": {
            k: {"markers": n, "removed": hit, "rate": ratio(hit, n)}
            for k, (hit, n) in sorted(by_category.items())
        },
        "leaks": leaks,
    }


def fairness_metrics(cases: list[Case]) -> dict[str, Any]:
    by_id = {c.spec.record.id: c for c in cases}
    pairs = []
    for case in cases:
        info = case.spec.record.fairness
        if info is None or info.role != "variant":
            continue
        base = by_id.get(f"{info.pair_id}-base")
        if base is None:
            continue
        pairs.append((base, case, info.dimension))
    same_words = same_bytes = 0
    deltas: list[float] = []
    by_dimension: dict[str, list[float]] = defaultdict(list)
    for base, variant, dimension in pairs:
        if base.blinded is None or variant.blinded is None:
            continue
        same_words += int(base.blinded.text.split() == variant.blinded.text.split())
        same_bytes += int(base.blinded.text == variant.blinded.text)
        if base.scoring is not None and variant.scoring is not None:
            delta = abs(base.scoring.overall - variant.scoring.overall)
            deltas.append(delta)
            by_dimension[str(dimension)].append(delta)
    fairness_cases = [c for c in cases if c.spec.record.kind == "fairness"]
    regular = [c for c in cases if c.spec.record.kind == "cv"]
    return {
        "pairs": len(pairs),
        "blind_marker_removal": {
            "form_contact_only": {
                "what": "Sources: the application form's contact details and patterns only.",
                "fairness_cvs": _blind_rate(fairness_cases, with_profile=False),
                "regular_cvs": _blind_rate(regular, with_profile=False),
            },
            "with_parsed_profile": {
                "what": (
                    "Sources: the form and the parsed CV profile (scripted from the ground "
                    "truth, so this assumes a correct parse)."
                ),
                "fairness_cvs": _blind_rate(fairness_cases, with_profile=True),
                "regular_cvs": _blind_rate(regular, with_profile=True),
            },
        },
        "blinded_input_identical_rate": ratio(same_words, len(pairs)),
        "blinded_input_byte_identical_rate": ratio(same_bytes, len(pairs)),
        "identical_input_note": (
            "Identical apart from where a PDF wraps a line (a longer name or email moves "
            "a line break)."
        ),
        "pipeline_overall_delta_scripted": {
            "what": (
                "Overall score difference across each pair with scripted answers: tests "
                "the deterministic part (redaction, evidence, aggregation), not a model."
            ),
            "max": max(deltas) if deltas else None,
            "mean": round(statistics.mean(deltas), 4) if deltas else None,
            "per_dimension_max": {k: max(v) for k, v in sorted(by_dimension.items())},
        },
        "aggregation_identical_for_identical_scores": _aggregation_check(cases),
        "model_score_delta": pending("needs model scores on the counterfactual pairs"),
    }


def _aggregation_check(cases: list[Case]) -> float | None:
    """For every scored case, the overall score recomputed from its criterion scores in
    a different order, and from a copy, is identical (deterministic aggregation)."""
    from pairly_hire.scoring.aggregate import overall_score
    from synth.hire.screening import screening_rubric

    checked = same = 0
    for case in cases:
        if case.scoring is None:
            continue
        rubric = screening_rubric(job_for(case.spec))
        scores = case.scoring.scores
        checked += 1
        same += int(
            overall_score(rubric, dict(reversed(list(scores.items()))))
            == overall_score(rubric, dict(scores))
            == case.scoring.overall
        )
    return ratio(same, checked)


def injection_metrics(cases: list[Case]) -> dict[str, Any]:
    leaks_det: list[str] = []
    det_payloads = cls_payloads = 0
    cls_leaks_without_classifier: list[str] = []
    for case in cases:
        attack = case.spec.record.attack
        if attack is None or not attack.payloads or case.blinded is None:
            continue
        model_input = normalize(case.blinded.text).text
        if attack.caught_by == "classifier":
            # Without the classifier (off, or a model that misses it): what would leak.
            report = pipeline.scan(case.doc, upload_name(case.spec))
            guarded = pipeline.guard(case.doc, report)
            blinded = pipeline.blind_input(guarded, contact_for(case.spec), None, BlindConfig())
            for payload in attack.payloads:
                if not payload:
                    continue
                cls_payloads += 1
                if normalize(payload).text in normalize(blinded.text).text:
                    cls_leaks_without_classifier.append(case.spec.record.id)
            continue
        for payload in attack.payloads:
            if not payload:
                continue
            det_payloads += 1
            if normalize(payload).text in model_input:
                leaks_det.append(case.spec.record.id)
    guardrails: dict[str, Any] = {}
    if GUARDRAILS.exists():
        data = json.loads(GUARDRAILS.read_text(encoding="utf-8"))
        guardrails = {
            "source": "evals/results/guardrails_hire.json",
            "document_level": data.get("document_level"),
            "type_level_recall": data.get("type_level_recall"),
            "payload_leaks_after_sanitize": data.get("payload_leaks"),
        }
    return {
        "guardrails": guardrails,
        "scoring_input_payload_leaks": {
            "what": (
                "Attack payloads found in the text the scoring model reads (after "
                "guardrails, sanitising and blind redaction)."
            ),
            "deterministic_scope": {
                "payloads": det_payloads,
                "leaks": len(leaks_det),
                "documents": sorted(set(leaks_det)),
            },
            "classifier_scope": pending(
                "paraphrased instructions are removed only by the model classifier; "
                "scripted classifier answers prove the plumbing, not detection"
            ),
            "classifier_scope_without_classifier": {
                "payloads": cls_payloads,
                "leaks": len(cls_leaks_without_classifier),
                "documents": sorted(set(cls_leaks_without_classifier)),
            },
        },
    }


def claim_metrics(cases: list[Case]) -> dict[str, Any]:
    attacks = [c for c in cases if c.spec.record.attack and c.spec.record.attack.claim_flags]
    hit = 0
    missed: list[str] = []
    for case in attacks:
        attack = case.spec.record.attack
        if attack is None:  # pragma: no cover - filtered above
            continue
        codes = {f.code for f in case.flags}
        ok = set(attack.claim_flags) <= codes
        hit += int(ok)
        if not ok:
            missed.append(case.spec.record.id)
    clean = [c for c in cases if c.spec.record.kind == "cv"]
    flagged: Counter[str] = Counter()
    for case in clean:
        flagged.update({f.code for f in case.flags if f.kind == "claim"})
    signals: Counter[str] = Counter()
    for case in clean:
        signals.update({f.code for f in case.flags if f.kind == "ai_signal"})
    return {
        "what": (
            "Claim checks on the parsed profile (scripted from the ground truth, so this "
            "assumes a correct parse). Flags are information only."
        ),
        "attacks": len(attacks),
        "recall": ratio(hit, len(attacks)),
        "missed": missed,
        "clean_cvs": len(clean),
        "clean_flag_rate_per_code": {
            code: ratio(n, len(clean)) for code, n in sorted(flagged.items())
        },
        "ai_signal_rate_per_code": {code: ratio(n, len(clean)) for code, n in signals.items()},
    }


def knockout_metrics(cases: list[Case]) -> dict[str, Any]:
    agree = total = rejections = 0
    for case in cases:
        job = job_for(case.spec)
        labels = next(lab for lab in case.spec.record.labels if lab.job_id == job.id)
        checks = check_knockouts(job.rubric, labels.knockout_answers)
        route = "pass" if all(c.matches for c in checks) else "human_review"
        total += 1
        agree += int(route == labels.knockout_route)
        rejections += sum(1 for c in checks if "reject" in str(c.suggested_action or ""))
    return {"applications": total, "route_agreement": ratio(agree, total), "rejections": rejections}


def latency_metrics(cases: list[Case]) -> dict[str, Any]:
    stages: dict[str, list[float]] = defaultdict(list)
    for case in cases:
        for stage, seconds in case.timings.items():
            stages[stage].append(seconds)

    def summary(values: list[float]) -> dict[str, float | None]:
        ordered = sorted(values)
        p95 = ordered[min(len(ordered) - 1, int(0.95 * len(ordered)))] if ordered else None
        return {
            "mean_ms": round(1000 * statistics.mean(values), 2) if values else None,
            "p95_ms": round(1000 * p95, 2) if p95 is not None else None,
        }

    return {
        "what": "Deterministic stages on this machine, per CV, in process (not gating).",
        "stages": {k: summary(v) for k, v in sorted(stages.items())},
        "model_latency": pending("needs live model calls"),
        "model_cost_usd_per_cv": pending("needs live model calls (recordings cost 0)"),
    }


def recordings_metrics(specs: dict[str, DocSpec]) -> dict[str, Any]:
    """The committed demo bundle answers every request the pipeline makes today."""
    from pairly_core.llm.bundle import parse_bundle

    router = BundleRouter.of(parse_bundle(recordings_text()))
    job = job_by_id(DEMO_JOB)
    complete = 0
    # The recorded demo CVs plus the three extra CVs ``make demo`` applies.
    todo = [specs[doc_id] for doc_id in DEMO_APPLICATIONS]
    todo += demo_extras(list(specs.values())).all()
    for spec in todo:
        case = asyncio.run(screen(spec, job, router=router))
        complete += int(
            case.scoring is not None and case.profile is not None and case.profile.parsed
        )
    return {
        "bundle": "synth/hire/recordings/hire_screening.json",
        "applications": len(todo),
        "answered": complete,
        "current": complete == len(todo),
        "note": SCRIPTED_NOTE,
    }


def counterfactual_runner_metrics(
    cases: list[Case], org_runs: Path = ORG_FAIRNESS_RUNS
) -> dict[str, Any]:
    """The fairness runner's own copies (``pairly_hire.screening.counterfactual``) over the
    base CV of every synthetic fairness pair: swapped names, gender markers and
    nationalities, then the same guardrails and blind redaction. Measured: whether the
    redaction hid every inserted value and whether the scoring input stayed identical.
    The model's score change needs a live model (``pending_live_run``). An organisation's
    exported run summaries are included when ``evals/results/fairness_runs.json`` exists."""
    copies = complete = identical = 0
    per: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0])
    leaks: list[str] = []
    bases = [
        c
        for c in cases
        if c.spec.record.fairness is not None
        and c.spec.record.fairness.role == "base"
        and c.guarded is not None
    ]
    for case in bases:
        sources = BlindSources.build(contact_for(case.spec), case.profile)
        app_id = uuid.uuid5(uuid.NAMESPACE_URL, f"pairly-evals:{case.spec.record.id}")
        text = case.guarded.text
        for swap in counterfactual.make_swaps(text, sources.names, sources.emails, app_id):
            check = counterfactual.check_copy(text, sources, BlindConfig(), swap)
            copies += 1
            complete += int(check.redaction_complete)
            identical += int(check.input_identical)
            stats = per[swap.dimension]
            stats[0] += 1
            stats[1] += int(check.redaction_complete)
            stats[2] += int(check.input_identical)
            if check.leaked and len(leaks) < 30:
                leaks.append(f"{case.spec.record.id}:{swap.dimension}")
    out: dict[str, Any] = {
        "what": (
            "The runner's counterfactual copies of each fairness base CV, through the same "
            "guardrails and blind redaction as production."
        ),
        "bases": len(bases),
        "copies": copies,
        "redaction_complete_rate": ratio(complete, copies),
        "input_identical_rate": ratio(identical, copies),
        "per_dimension": {
            k: {"copies": n, "redaction_complete": c, "input_identical": i}
            for k, (n, c, i) in sorted(per.items())
        },
        "leaks": leaks,
        "model_score_delta": pending(
            "the copies' score change needs a live model; recordings answer only inputs "
            "they were recorded for"
        ),
    }
    if org_runs.is_file():
        try:
            out["org_runs"] = json.loads(org_runs.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            out["org_runs"] = {"status": "unreadable", "path": str(org_runs)}
    return out


# ------------------------------------------------------------------ main


def run(count: int, seed: int, include_scans: bool, limit: int | None) -> dict[str, Any]:
    started = time.monotonic()
    specs = build(count, seed)
    chosen = [s for s in specs if include_scans or s.record.format not in SCANS]
    if limit is not None:
        chosen = chosen[:limit]
    collected = collect(chosen)
    cases = collected.cases
    result = {
        "generated_by": "python -m evals.hire",
        "dataset": {
            "generator": "synth.hire",
            "count": count,
            "seed": seed,
            "scans_included": include_scans,
            "documents": len(chosen),
            "screened": len(cases),
            "errors": collected.errors,
            "error_count": len(collected.errors),
        },
        "model_answers": (
            "scripted from synthetic ground truth (synth.hire.screening); not model output. "
            "Every model-dependent metric is pending_live_run."
        ),
        "replay_deterministic": not collected.replay_mismatches,
        "replay_mismatches": collected.replay_mismatches,
        "extraction": extraction_metrics(cases),
        "evidence_validity": evidence_metrics(cases),
        "rubric_agreement": pending(
            "agreement with the synthetic labels is only meaningful for a live model: the "
            "recordings are scripted from those labels"
        ),
        "scripted_credit": credit_metrics(cases),
        "fairness": fairness_metrics(cases),
        "counterfactual_runner": counterfactual_runner_metrics(cases),
        "injection": injection_metrics(cases),
        "claims": claim_metrics(cases),
        "knockouts": knockout_metrics(cases),
        "latency_cost": latency_metrics(cases),
        "recordings": recordings_metrics({s.record.id: s for s in specs}),
    }
    result["seconds"] = round(time.monotonic() - started, 1)
    return result


def main(argv: list[str] | None = None) -> int:
    from evals.hire.report import write_report

    parser = argparse.ArgumentParser(prog="python -m evals.hire")
    parser.add_argument("--count", type=int, default=DEFAULT_COUNT)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--include-scans", action="store_true")
    parser.add_argument("--limit", type=int, default=None, help="first N documents only")
    parser.add_argument("--out", type=Path, default=RESULTS)
    parser.add_argument("--report", type=Path, default=REPORT)
    args = parser.parse_args(argv)
    result = run(args.count, args.seed, args.include_scans, args.limit)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    write_report(args.report, result)
    print(
        json.dumps(
            {
                "documents": result["dataset"]["documents"],
                "errors": len(result["dataset"]["errors"]),
                "evidence_validity": result["evidence_validity"]["rate"],
                "blind_marker_removal": result["fairness"]["blind_marker_removal"][
                    "form_contact_only"
                ]["fairness_cvs"]["rate"],
                "seconds": result["seconds"],
            },
            indent=1,
        )
    )
    print(f"wrote {args.out} and {args.report}")
    return 0
