# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Rubric labels for synthetic CVs: a transparent, deterministic rule per criterion.

These are SYNTHETIC labels, written by rules from the generated facts. They are not
human judgments and must never be presented as such. They stand in for "human-style"
labels (a score 0 to 4 per criterion plus the CV text that justifies it) so the
scoring evals have a reproducible reference; real calibration needs real reviewers
(brief 3.3 item 11).

Every label is computed from what the CV shows, never from the person's name, gender,
nationality, age or photo (the rules do not read ``Persona``), so counterfactual
fairness pairs get identical labels by construction.

The rules (``RULES`` maps each criterion of the three sample jobs to one):

* ``years(families)``: effective years = sum over jobs of months / 12 (claims read
  literally; a period that ends before it starts counts 0) times the
  family weight (1.0 for the job's own family, 0.5 for the listed related families).
  Score 0 if 0; 1 if under 1 year; 2 if under 3; 3 if under 5; 4 otherwise. Evidence:
  the title line and the date line of each counted job (most recent three).
* ``demonstrated(tags, skills, list_credit)``: an achievement line counts when its
  template demonstrates one of ``tags`` or uses one of ``skills``. Score 2, 3 or 4 for
  1, 2 or 3+ such lines (evidence: those lines, at most three). With none, a skill in
  ``skills`` named only in the skills list gives 1 when ``list_credit`` (evidence: the
  skills line), otherwise 0. No evidence, no credit.
* ``credential(certs, best_fields, related_fields)``: 4 for a listed certificate;
  else for the highest degree: 3 for a degree in ``best_fields`` (4 for a master's
  when ``master_bonus``), 2 for a degree in a related field, 1 for a diploma in either;
  else 0. Evidence: the certificate or education line.
* ``language(name)``: Native or Fluent 4, Professional 3, Conversational 2, Basic 1,
  not listed 0. Evidence: the languages line.

The overall score is the weighted mean of the criterion scores as a percentage of the
scale maximum (``overall_score``, imported from the plugin's
``pairly_hire.scoring.aggregate`` so the labels and the product share one formula).
Knockout answers are synthetic application answers
("yes" or "no", as the apply form takes them); one that differs from the rubric's
``expected_answer`` routes the CV to a person (``human_review``), never to a rejection.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Final, Literal

from pairly_hire.scoring.aggregate import overall_score
from synth.hire.facts import CvFacts
from synth.hire.models import CriterionLabel, Job, JobLabels
from synth.hire.text import CvText

MAX_EVIDENCE = 3
LANG_SCORES: Final = {"Native": 4, "Fluent": 4, "Professional": 3, "Conversational": 2, "Basic": 1}


@dataclass(frozen=True)
class Years:
    families: dict[str, float]

    kind = "years"

    @property
    def name(self) -> str:
        return "years(" + ",".join(f"{k}:{v}" for k, v in sorted(self.families.items())) + ")"

    def apply(self, facts: CvFacts, text: CvText) -> tuple[int, tuple[str, ...]]:
        months = 0.0
        evidence: list[str] = []
        for i, job in enumerate(facts.jobs):
            weight = self.families.get(job.family, 0.0)
            if weight <= 0:
                continue
            # A claimed period that ends before it starts counts as nothing.
            months += max(job.months, 0) * weight
            if len(evidence) < 2 * MAX_EVIDENCE:
                evidence += [line.text for line in text.by_ref(f"exp:{i}")]
                evidence += [line.text for line in text.by_ref(f"exp:{i}:dates")]
        years = months / 12
        if years <= 0:
            return 0, ()
        score = 1 if years < 1 else 2 if years < 3 else 3 if years < 5 else 4
        return score, tuple(evidence)


@dataclass(frozen=True)
class Demonstrated:
    tags: frozenset[str]
    skills: frozenset[str]
    list_credit: bool = True

    kind = "demonstrated"

    @property
    def name(self) -> str:
        return (
            f"demonstrated(tags={','.join(sorted(self.tags))};"
            f"skills={','.join(sorted(self.skills))};list_credit={self.list_credit})"
        )

    def apply(self, facts: CvFacts, text: CvText) -> tuple[int, tuple[str, ...]]:
        lines: list[str] = []
        for i, job in enumerate(facts.jobs):
            for j, h in enumerate(job.highlights):
                if self.tags & set(h.tags) or self.skills & set(h.skills):
                    lines += [line.text for line in text.by_ref(f"exp:{i}:h:{j}")]
        if lines:
            score = min(4, len(lines) + 1)
            return score, tuple(lines[:MAX_EVIDENCE])
        if self.list_credit and any(s in self.skills for s in facts.skills):
            # The skills line itself: a bare skill name ("Go") is too short to quote.
            return 1, tuple(line.text for line in text.by_ref("skills"))
        return 0, ()


@dataclass(frozen=True)
class Credential:
    certs: frozenset[str]
    best_fields: frozenset[str]
    related_fields: frozenset[str]
    master_bonus: bool = False

    kind = "credential"

    @property
    def name(self) -> str:
        return (
            f"credential(certs={','.join(sorted(self.certs))};"
            f"best={','.join(sorted(self.best_fields))};"
            f"related={','.join(sorted(self.related_fields))};master_bonus={self.master_bonus})"
        )

    def apply(self, facts: CvFacts, text: CvText) -> tuple[int, tuple[str, ...]]:
        for i, cert in enumerate(facts.certs):
            if cert.name in self.certs:
                return 4, tuple(line.text for line in text.by_ref(f"cert:{i}"))
        best: tuple[int, int] | None = None
        for i, edu in enumerate(facts.education):
            if edu.degree == "diploma":
                score = 1 if edu.field in self.best_fields | self.related_fields else 0
            elif edu.field in self.best_fields:
                score = 4 if self.master_bonus and edu.degree in ("master", "mba") else 3
            elif edu.field in self.related_fields:
                score = 2
            else:
                score = 0
            if score and (best is None or score > best[0]):
                best = (score, i)
        if best is None:
            return 0, ()
        return best[0], tuple(line.text for line in text.by_ref(f"edu:{best[1]}"))


@dataclass(frozen=True)
class Language:
    language: str

    kind = "language"

    @property
    def name(self) -> str:
        return f"language({self.language})"

    def apply(self, facts: CvFacts, text: CvText) -> tuple[int, tuple[str, ...]]:
        for name, level in facts.languages:
            if name == self.language:
                return LANG_SCORES.get(level, 0), tuple(line.text for line in text.by_ref("langs"))
        return 0, ()


Rule = Years | Demonstrated | Credential | Language

COMPUTING = frozenset({"Computer Science", "Software Engineering", "Computer Engineering"})
TECH_RELATED = frozenset({"Information Technology", "Mathematics", "Statistics"})

RULES: Final[dict[str, dict[str, Rule]]] = {
    "backend_engineer": {
        "backend_experience": Years({"backend": 1.0, "frontend": 0.5, "data_analyst": 0.5}),
        "python_or_go": Demonstrated(frozenset(), frozenset({"Python", "Go"})),
        "databases_apis": Demonstrated(
            frozenset({"api_design", "databases"}),
            frozenset({"PostgreSQL", "REST APIs", "gRPC"}),
        ),
        "cloud_containers": Demonstrated(
            frozenset({"cloud_ops"}), frozenset({"Docker", "Kubernetes", "AWS"})
        ),
        "distributed_systems": Demonstrated(
            frozenset({"distributed_systems", "system_design"}), frozenset({"Kafka"})
        ),
        "testing_quality": Demonstrated(
            frozenset({"testing"}), frozenset({"unit testing", "CI/CD", "Jest"})
        ),
        "mentoring": Demonstrated(
            frozenset({"mentoring", "leadership"}), frozenset(), list_credit=False
        ),
        "cs_degree": Credential(frozenset(), COMPUTING, TECH_RELATED, master_bonus=True),
    },
    "ap_accountant": {
        "ap_experience": Years({"accounts_payable": 1.0, "accountant": 0.5}),
        "invoice_matching": Demonstrated(
            frozenset({"invoice_processing", "three_way_match"}),
            frozenset({"invoice processing", "three-way matching"}),
        ),
        "erp_systems": Demonstrated(
            frozenset(), frozenset({"SAP", "Oracle Fusion", "Microsoft Dynamics"})
        ),
        "uae_vat": Demonstrated(frozenset({"vat"}), frozenset({"VAT"})),
        "reconciliation_close": Demonstrated(
            frozenset({"reconciliation", "close"}),
            frozenset({"vendor reconciliation", "bank reconciliation"}),
        ),
        "accounting_qualification": Credential(
            frozenset({"ACCA", "CMA", "CPA"}),
            frozenset({"Accounting"}),
            frozenset({"Finance", "Commerce", "Economics", "Business Administration"}),
        ),
        "arabic_language": Language("Arabic"),
    },
    "recruiter": {
        "recruiting_experience": Years({"recruiting": 1.0, "hr_generalist": 0.5}),
        "full_cycle": Demonstrated(
            frozenset({"full_cycle", "offers"}),
            frozenset({"full-cycle recruiting", "offer negotiation"}),
        ),
        "sourcing": Demonstrated(
            frozenset({"sourcing"}), frozenset({"sourcing", "LinkedIn Recruiter"})
        ),
        "tech_hiring": Demonstrated(frozenset({"tech_hiring"}), frozenset(), list_credit=False),
        "ats_process": Demonstrated(
            frozenset({"ats", "structured_interviews", "time_to_hire"}),
            frozenset({"Greenhouse", "Workday"}),
        ),
        "high_volume": Demonstrated(frozenset({"high_volume"}), frozenset(), list_credit=False),
        "arabic_language": Language("Arabic"),
        "hr_certification": Credential(
            frozenset({"SHRM-CP", "CIPD Level 5"}),
            frozenset({"Human Resources"}),
            frozenset({"Psychology", "Business Administration"}),
        ),
    },
}

ACCOUNTING_FIELDS: Final = frozenset({"Accounting", "Finance", "Commerce"})

KnockoutAnswer = Callable[[CvFacts], bool]
KNOCKOUT_ANSWERS: Final[dict[str, KnockoutAnswer]] = {
    "right_to_work_uae": lambda f: f.right_to_work_uae,
    "start_within_60_days": lambda f: f.notice_period_days <= 60,
    "accounting_degree": lambda f: any(
        e.field in ACCOUNTING_FIELDS and e.degree != "diploma" for e in f.education
    ),
    "travel_ksa": lambda f: f.travel_ksa,
}


def label_job(job: Job, facts: CvFacts, text: CvText) -> JobLabels:
    rules = RULES[job.id]
    labels: list[CriterionLabel] = []
    for criterion in job.rubric.criteria:
        rule = rules[criterion.id]
        score, evidence = rule.apply(facts, text)
        if score == 0:
            evidence = ()
        labels.append(
            CriterionLabel(
                criterion_id=criterion.id, score=score, evidence=evidence, rule=rule.kind
            )
        )
    knockouts = job.rubric.knockout_questions
    answers: dict[str, Literal["yes", "no"]] = {
        k.id: "yes" if KNOCKOUT_ANSWERS[k.id](facts) else "no" for k in knockouts
    }
    passed = all(answers[k.id] == k.expected_answer for k in knockouts)
    return JobLabels(
        job_id=job.id,
        rubric_version=job.rubric_version,
        criteria=tuple(labels),
        overall=overall_score(job.rubric, {lab.criterion_id: lab.score for lab in labels}),
        knockout_answers=answers,
        knockout_route="pass" if passed else "human_review",
    )


def label_all(jobs: list[Job], facts: CvFacts, text: CvText) -> tuple[JobLabels, ...]:
    return tuple(label_job(job, facts, text) for job in jobs)


def rule_table() -> dict[str, dict[str, str]]:
    """Every criterion's rule with its parameters (written to the manifest summary)."""
    return {job: {crit: rule.name for crit, rule in rules.items()} for job, rules in RULES.items()}
