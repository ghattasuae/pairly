# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""The bias and adverse-impact report and its audit pack (brief 3.10).

For each voluntary self-identification category and each step of the pipeline (from
one stage to the next, in board order, ending at ``hired``), per group:

* ``base``: applications of the group that reached the earlier stage (or a later one);
* ``selected``: those that also reached the later stage; ``rate`` = selected / base;
* ``impact_ratio``: the group's rate over the highest rate among the compared groups
  (the reference group). Below 0.8 is under the four-fifths rule of thumb.

Rules, all configurable where it says so:

* Suppression: a group with fewer than ``MIN_GROUP`` (5) applications at the base is
  hidden (no counts, no rate) and never used as the reference.
* Small samples: a group (or the reference) with fewer than ``min_sample`` (default 30)
  at the base gets a Fisher exact test against the reference (two-sided, standard
  library, ``stats.fisher_exact``). If p is 0.05 or more the step reads "too small to
  judge"; below 0.05 the ratio is reported with that p-value.
* "Prefer not to say" and unanswered are counted and shown, never compared.

The self-identification answers are read only through ``pairly_hire.selfid.reporting``
(counts per group); this module never sees which candidate gave which answer, and the
scoring code never imports either. The report is aggregate data: no application id,
no reference, no name.

``audit_pack`` builds the export: ``data.csv`` (the counts), ``results.csv`` (rates,
ratios, tests and judgements), ``report.json`` (everything, with the scope and the
settings) and ``METHOD.md`` (how it was computed). It supports an independent audit, such
as one under New York City Local Law 144; it does not by itself show compliance with any
law.
"""

from __future__ import annotations

import csv
import hashlib
import io
import itertools
import json
import uuid
import zipfile
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime
from typing import Any, Final

from sqlalchemy.ext.asyncio import AsyncSession

from pairly_hire.analytics.facts import AppFact, load_pipeline
from pairly_hire.analytics.metrics import active_keys, reached
from pairly_hire.analytics.stats import fisher_exact
from pairly_hire.pipeline.stages import DEFAULT_STAGES, HIRED, StageSet
from pairly_hire.selfid import reporting

MIN_GROUP: Final = 5
DEFAULT_MIN_SAMPLE: Final = 30
FOUR_FIFTHS: Final = 0.8
ALPHA: Final = 0.05
NOT_COMPARED: Final = frozenset({reporting.PREFER_NOT, reporting.UNANSWERED})


@dataclass(frozen=True, slots=True)
class BiasSettings:
    min_sample: int = DEFAULT_MIN_SAMPLE
    min_group: int = MIN_GROUP


@dataclass(slots=True)
class GroupResult:
    group: str
    label_en: str
    label_ar: str
    suppressed: bool
    base: int | None
    selected: int | None
    rate: float | None
    impact_ratio: float | None
    small_sample: bool
    fisher_p: float | None
    judgement: str


@dataclass(slots=True)
class StepResult:
    from_stage: str
    to_stage: str
    reference_group: str | None
    groups: list[GroupResult]


@dataclass(slots=True)
class CategoryResult:
    category: str
    label_en: str
    label_ar: str
    steps: list[StepResult]


@dataclass(slots=True)
class BiasReport:
    generated_at: str
    job_id: str | None
    since: str | None
    until: str | None
    applications: int
    settings: BiasSettings
    stages: list[str]
    categories: list[CategoryResult]
    fairness: dict[str, Any] | None = None
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def stage_order(stage_sets: Sequence[StageSet]) -> list[str]:
    order = active_keys(DEFAULT_STAGES)
    for stages in stage_sets:
        for key in active_keys(stages):
            if key not in order:
                order.append(key)
    return [*order, HIRED]


def reached_stages(
    apps: Sequence[AppFact], stages: Mapping[uuid.UUID, StageSet], order: Sequence[str]
) -> dict[uuid.UUID, set[str]]:
    return {
        a.id: {k for k in order if reached(a, stages.get(a.job_id, DEFAULT_STAGES), k)}
        for a in apps
    }


def _judge(
    group: str,
    base: int,
    selected: int,
    ref: tuple[str, int, int] | None,
    settings: BiasSettings,
) -> tuple[float | None, bool, float | None, str]:
    """(impact_ratio, small_sample, fisher_p, judgement) of one compared group."""
    if ref is None or group in NOT_COMPARED:
        return None, base < settings.min_sample, None, "not_compared"
    ref_group, ref_base, ref_selected = ref
    rate, ref_rate = selected / base, ref_selected / ref_base
    ratio = round(rate / ref_rate, 4) if ref_rate > 0 else None
    small = base < settings.min_sample or ref_base < settings.min_sample
    if group == ref_group:
        return 1.0 if ref_rate > 0 else None, small, None, "reference"
    p: float | None = None
    if small:
        p = round(fisher_exact(selected, base - selected, ref_selected, ref_base - ref_selected), 6)
    if ratio is None:
        return None, small, p, "not_compared"
    if small and (p is None or p >= ALPHA):
        return ratio, small, p, "too_small_to_judge"
    if ratio < FOUR_FIFTHS:
        return ratio, small, p, "below_four_fifths"
    return ratio, small, p, "meets_four_fifths"


def step_result(
    counts: Mapping[str, Mapping[str, int]],
    from_stage: str,
    to_stage: str,
    labels: Mapping[str, tuple[str, str]],
    settings: BiasSettings,
) -> StepResult:
    rows: list[tuple[str, int, int]] = []
    for group in sorted(counts):
        base = int(counts[group].get(from_stage, 0))
        if base == 0:
            continue
        rows.append((group, base, int(counts[group].get(to_stage, 0))))
    compared = [r for r in rows if r[0] not in NOT_COMPARED and r[1] >= settings.min_group]
    ref = max(compared, key=lambda r: (r[2] / r[1], r[1], r[0]), default=None)
    out: list[GroupResult] = []
    for group, base, selected in rows:
        en, ar = labels.get(group, (group, group))
        if base < settings.min_group:
            out.append(
                GroupResult(group, en, ar, True, None, None, None, None, True, None, "suppressed")
            )
            continue
        ratio, small, p, judgement = _judge(group, base, selected, ref, settings)
        out.append(
            GroupResult(
                group,
                en,
                ar,
                False,
                base,
                selected,
                round(selected / base, 4),
                ratio,
                small,
                p,
                judgement,
            )
        )
    return StepResult(from_stage, to_stage, ref[0] if ref else None, out)


def category_labels(category: str) -> dict[str, tuple[str, str]]:
    found = reporting.categories().get(category)
    labels = {o.id: (o.label_en, o.label_ar) for o in found.options} if found else {}
    labels[reporting.UNANSWERED] = ("Not answered", "لم يجب")
    return labels


def build(
    counts_by_category: Mapping[str, Mapping[str, Mapping[str, int]]],
    order: Sequence[str],
    *,
    applications: int,
    settings: BiasSettings,
    scope: Mapping[str, str | None],
    now: datetime,
) -> BiasReport:
    cats = reporting.categories()
    results: list[CategoryResult] = []
    for category, counts in counts_by_category.items():
        meta = cats.get(category)
        labels = category_labels(category)
        steps = [
            step_result(counts, here, there, labels, settings)
            for here, there in itertools.pairwise(order)
        ]
        results.append(
            CategoryResult(
                category,
                meta.label_en if meta else category,
                meta.label_ar if meta else category,
                [s for s in steps if s.groups],
            )
        )
    return BiasReport(
        generated_at=now.isoformat(),
        job_id=scope.get("job_id"),
        since=scope.get("since"),
        until=scope.get("until"),
        applications=applications,
        settings=settings,
        stages=list(order),
        categories=results,
    )


async def build_report(
    session: AsyncSession,
    *,
    job_id: uuid.UUID | None = None,
    since: date | None = None,
    until: date | None = None,
    settings: BiasSettings | None = None,
) -> BiasReport:
    settings = settings or BiasSettings()
    apps, stages = await load_pipeline(session, job_id=job_id, since=since, until=until)
    order = stage_order(list(stages.values()))
    stage_reached = reached_stages(apps, stages, order)
    counts = {
        category: await reporting.stage_counts(session, stage_reached, category=category)
        for category in reporting.categories()
    }
    return build(
        counts,
        order,
        applications=len(apps),
        settings=settings,
        scope={
            "job_id": str(job_id) if job_id else None,
            "since": since.isoformat() if since else None,
            "until": until.isoformat() if until else None,
        },
        now=datetime.now(UTC),
    )


# ------------------------------------------------------------------ the audit pack

METHOD_NOTE: Final = """\
# Bias audit pack: method

Produced by Pairly Hire from the organisation's own records. It supports an independent
bias audit, such as one under New York City Local Law 144. It does not by itself show
compliance with any law, and it is not legal advice.

## Data
* Applications submitted in the scope below (job and dates), counted once each.
* Voluntary self-identification answers, given by candidates on a separate part of the
  form. They are stored apart, encrypted, and never used in scoring.
* For each pipeline step (from one stage to the next, in board order, ending at hired),
  the number of applications of each group that reached the earlier stage (base) and the
  number that also reached the later one (selected). Reaching a stage means reaching it
  or any later stage.

## Results
* Selection rate: selected divided by base, per group.
* Impact ratio: a group's selection rate divided by the highest selection rate among the
  compared groups (the reference group). Under 0.8 is below the four-fifths rule of thumb.
* "Prefer not to say" and "Not answered" are counted but never compared.

## Small samples and privacy
* Groups with fewer than {min_group} applications at the base are suppressed: no counts,
  no rate, never the reference.
* When a group or the reference has fewer than {min_sample} applications at the base, a
  two-sided Fisher exact test compares the two. If p is 0.05 or more the result reads
  "too small to judge"; otherwise the ratio is reported with its p-value.
* The pack holds counts only: no names, no application references, no single answers.

## Files
* data.csv: counts per category, step and group.
* results.csv: rates, impact ratios, Fisher p-values and judgements.
* report.json: everything above with the scope and the settings.
"""

DATA_HEADER: Final = (
    "category",
    "from_stage",
    "to_stage",
    "group",
    "base",
    "selected",
    "suppressed",
)
RESULTS_HEADER: Final = (
    "category",
    "from_stage",
    "to_stage",
    "group",
    "selection_rate",
    "reference_group",
    "impact_ratio",
    "small_sample",
    "fisher_p",
    "judgement",
)


def _csv(header: Sequence[str], rows: Sequence[Sequence[Any]]) -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(header)
    writer.writerows(["" if v is None else v for v in row] for row in rows)
    return buffer.getvalue().encode("utf-8")


def _cell(value: Any) -> Any:
    """Never start a cell with a formula character (CSV injection in spreadsheets)."""
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@"):
        return "'" + value
    return value


def audit_pack(report: BiasReport) -> bytes:
    """The zip: data.csv, results.csv, report.json and METHOD.md (stable order)."""
    data: list[list[Any]] = []
    results: list[list[Any]] = []
    for cat in report.categories:
        for step in cat.steps:
            for g in step.groups:
                data.append(
                    [
                        _cell(cat.category),
                        _cell(step.from_stage),
                        _cell(step.to_stage),
                        _cell(g.group),
                        g.base,
                        g.selected,
                        g.suppressed,
                    ]
                )
                results.append(
                    [
                        _cell(cat.category),
                        _cell(step.from_stage),
                        _cell(step.to_stage),
                        _cell(g.group),
                        g.rate,
                        _cell(step.reference_group),
                        g.impact_ratio,
                        g.small_sample,
                        g.fisher_p,
                        g.judgement,
                    ]
                )
    method = METHOD_NOTE.format(
        min_group=report.settings.min_group, min_sample=report.settings.min_sample
    )
    scope = (
        f"\n## Scope\n* Job: {report.job_id or 'all jobs'}\n* Applied from: "
        f"{report.since or 'the start'}\n* Applied before: {report.until or 'now'}\n"
        f"* Applications: {report.applications}\n* Generated: {report.generated_at}\n"
    )
    files = {
        "data.csv": _csv(DATA_HEADER, data),
        "results.csv": _csv(RESULTS_HEADER, results),
        "report.json": json.dumps(report.as_dict(), indent=2, ensure_ascii=False).encode("utf-8"),
        "METHOD.md": (method + scope).encode("utf-8"),
    }
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, content)
    return out.getvalue()


def pack_digest(pack: bytes) -> str:
    return hashlib.sha256(pack).hexdigest()
