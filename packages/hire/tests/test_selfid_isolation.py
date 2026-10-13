# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Scoring can never see voluntary self-identification answers (brief 3.2.1).

Three independent checks:

1. Imports: no module of ``pairly_hire.scoring`` or ``pairly_hire.screening`` imports,
   directly or through other Hire modules, ``pairly_hire.selfid`` or the modules that
   know self-ID categories (the import-linter contract in the root ``pyproject.toml``
   says the same).
2. Schema: ``ScoringInput`` has no field named after a self-ID category, and the
   ``hire_applications`` table has no such column.
3. Data flow: the stored application answers (``CheckedAnswers.stored``) only ever hold
   custom and knockout answers.
"""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

from pairly_hire.demo import BACKEND
from pairly_hire.forms import DEFAULT_FORM, check_answers
from pairly_hire.models import HireApplication, HireCandidate
from pairly_hire.scoring.inputs import ALLOWED_FIELDS, ScoringInput
from pairly_hire.selfid.schema import CATEGORIES

FORBIDDEN = ("pairly_hire.selfid", "pairly_hire.forms", "pairly_hire.applying", "pairly_hire.api")


def _module_path(name: str) -> Path | None:
    try:
        spec = importlib.util.find_spec(name)
    except ModuleNotFoundError:  # ``pkg.module.Name``: a name imported from a module
        return None
    return Path(spec.origin) if spec and spec.origin else None


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
            found |= {f"{node.module}.{alias.name}" for alias in node.names}
    return found


def _closure(start: list[str]) -> set[str]:
    """Every ``pairly_hire`` module reachable from ``start`` through imports."""
    seen: set[str] = set()
    todo = list(start)
    while todo:
        name = todo.pop()
        if name in seen or not name.startswith("pairly_hire"):
            continue
        path = _module_path(name)
        if path is None or path.suffix != ".py":
            continue
        seen.add(name)
        todo.extend(i for i in _imports(path) if i.startswith("pairly_hire"))
    return seen


def test_scoring_package_cannot_reach_self_identification() -> None:
    package = Path(__file__).resolve().parents[1] / "pairly_hire"
    modules = [
        f"pairly_hire.{pkg}.{p.stem}"
        for pkg in ("scoring", "screening")
        for p in (package / pkg).glob("*.py")
        if p.stem != "__init__"
    ]
    reachable = _closure(["pairly_hire.scoring", "pairly_hire.screening", *modules])
    assert "pairly_hire.scoring.inputs" in reachable
    assert "pairly_hire.screening.graph" in reachable
    leaked = {m for m in reachable if m.startswith(FORBIDDEN)}
    assert leaked == set(), f"scoring or screening reaches {sorted(leaked)}"


def test_scoring_input_has_no_self_identification_field() -> None:
    names = {name.lower() for name in ALLOWED_FIELDS}
    assert not names & set(CATEGORIES)
    assert not any("self" in n or "gender" in n or "age" in n.split("_") for n in names)
    assert set(ScoringInput.__dataclass_fields__) == ALLOWED_FIELDS


def test_application_and_candidate_tables_hold_no_self_identification() -> None:
    for model in (HireApplication, HireCandidate):
        columns = set(model.__table__.columns.keys())
        assert not columns & set(CATEGORIES)
        assert not any("self" in c or "gender" in c or "disab" in c for c in columns)


def test_stored_answers_never_carry_self_identification() -> None:
    checked = check_answers(DEFAULT_FORM, BACKEND.rubric, {}, {"right_to_work_uae": "yes"})
    stored = checked.stored()
    assert set(stored) == {"questions", "knockouts"}
    assert not set(stored["questions"]) & set(CATEGORIES)


def test_only_the_portal_export_reads_a_candidates_own_answers() -> None:
    """``pairly_hire.selfid.export`` (a candidate's own answers, for their data export)
    has exactly one importer: the portal service. Bias reporting reads the table itself,
    as aggregates only."""
    package = Path(__file__).resolve().parents[1] / "pairly_hire"
    importers = set()
    for path in package.rglob("*.py"):
        if "migrations" in path.parts:
            continue
        if any(
            i == "pairly_hire.selfid.export" or i.startswith("pairly_hire.selfid.export.")
            for i in _imports(path)
        ):
            importers.add(".".join(path.relative_to(package.parent).with_suffix("").parts))
    assert importers == {"pairly_hire.portal.service"}


def test_only_the_bias_report_reads_counts_per_group() -> None:
    """``pairly_hire.selfid.reporting`` (counts per group, never a row) has exactly one
    importer: the bias report. Analytics, fairness and calibration never import it."""
    package = Path(__file__).resolve().parents[1] / "pairly_hire"
    importers = set()
    for path in package.rglob("*.py"):
        if "migrations" in path.parts:
            continue
        if any(
            i == "pairly_hire.selfid.reporting" or i.startswith("pairly_hire.selfid.reporting.")
            for i in _imports(path)
        ):
            importers.add(".".join(path.relative_to(package.parent).with_suffix("").parts))
    assert importers == {"pairly_hire.analytics.bias"}
    reachable = _closure(
        [
            "pairly_hire.analytics.facts",
            "pairly_hire.analytics.metrics",
            "pairly_hire.analytics.stats",
        ]
    )
    assert not {m for m in reachable if m.startswith("pairly_hire.selfid")}
