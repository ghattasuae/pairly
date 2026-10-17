# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""The counterfactual fairness runner on synthetic fairness pairs: the swaps, the
redaction check on the copies, and the model step (pending unless live)."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from pairly_core.llm.settings import ModelMode
from pairly_core.workflow.services import GraphServices
from pairly_hire.analytics.fairness import model_mode
from pairly_hire.screening import counterfactual as cf
from pairly_hire.screening.blind import BlindConfig
from pairly_hire.screening.counterfactual import PairScore, check_copy, make_swaps

APP = uuid.UUID(int=7)
CV = """Ravi Menon
ravi.menon@example.net | +971 50 123 4567 | ravi-menon.example.com
Nationality: Indian
Gender: Male

Summary
Mr. Menon is a backend engineer. He built APIs in Go for the Indian market.

Languages
Hindi (Native), English (Fluent), Indian Sign Language (Basic)

Experience
Backend Engineer, Mendoza Group, 2021 to 2026
Ravi led the move to Kubernetes and cut his team's deploy time by 40%.
"""
CONTACT = {"full_name": "Ravi Menon", "email": "ravi.menon@example.net"}


def swaps() -> dict[str, cf.Swap]:
    return {s.dimension: s for s in make_swaps(CV, ["Ravi Menon"], [CONTACT["email"]], APP)}


def test_three_swaps_with_synthetic_values_only() -> None:
    found = swaps()
    assert set(found) == {"name", "gender", "nationality"}
    for swap in found.values():
        for value in swap.inserted:
            assert "ravi" not in value.lower()
            assert "menon" not in value.lower()
    assert found["nationality"].inserted[0] != "Indian"
    # Deterministic per application.
    assert swaps()["name"].inserted == found["name"].inserted


def test_copies_change_the_attribute_and_nothing_else() -> None:
    found = swaps()
    name = cf.apply(CV, found["name"])
    first, last, email = found["name"].inserted
    assert "Ravi" not in name
    assert "Menon" not in name
    assert f"{first} {last}" in name
    assert email in name
    # A name inside a lowercase link stays lowercase.
    assert f"{first.lower()}-{last.lower()}.example.com" in name
    assert "Mendoza Group" in name
    gender = cf.apply(CV, found["gender"])
    assert "Gender: Female" in gender
    assert "Ms. " in gender
    assert "She built" in gender
    assert "her team's" in gender
    nationality = cf.apply(CV, found["nationality"])
    new = found["nationality"].inserted[0]
    assert f"Nationality: {new}" in nationality
    assert f"the {new} market" in nationality
    # A language is a skill: "Indian Sign Language" stays.
    assert "Indian Sign Language (Basic)" in nationality


def test_redaction_hides_every_inserted_value_and_the_input_stays_identical() -> None:
    sources = cf.sources_for(CONTACT, None)
    for swap in swaps().values():
        check = check_copy(CV, sources, BlindConfig(), swap)
        assert check.leaked == (), swap.dimension
        assert check.redaction_complete
        assert check.input_identical, swap.dimension


def test_blind_mode_off_leaks_and_says_so() -> None:
    sources = cf.sources_for(CONTACT, None)
    check = check_copy(CV, sources, BlindConfig(enabled=False), swaps()["name"])
    assert check.leaked
    assert not check.redaction_complete
    assert not check.input_identical


def test_no_stated_nationality_no_nationality_copy() -> None:
    text = "Sara Haddad\nBackend engineer since 2020.\n"
    found = make_swaps(text, ["Sara Haddad"], [], APP)
    assert [s.dimension for s in found] == ["name", "gender"]
    assert make_swaps("No name here.\n", [], [], APP) == []


async def test_model_step_is_pending_unless_live() -> None:
    check = check_copy(CV, cf.sources_for(CONTACT, None), BlindConfig(), swaps()["name"])
    pending = await cf.score_pair(check, None, None, live=False, org_id=APP)  # type: ignore[arg-type]
    assert pending == PairScore("pending_live_run")
    assert pending.delta is None


async def test_live_model_step_records_the_difference(monkeypatch: pytest.MonkeyPatch) -> None:
    check = check_copy(CV, cf.sources_for(CONTACT, None), BlindConfig(), swaps()["gender"])
    overall = iter([72.5, 64.0])

    async def fake_score(*args: Any, **kwargs: Any) -> Any:
        return SimpleNamespace(result=SimpleNamespace(overall=next(overall)))

    monkeypatch.setattr("pairly_hire.screening.pipeline.score_cv", fake_score)
    scored = await cf.score_pair(check, object(), object(), live=True, org_id=APP)  # type: ignore[arg-type]
    assert (scored.status, scored.control, scored.copy, scored.delta) == ("scored", 72.5, 64.0, 8.5)


async def test_a_model_failure_is_a_status_never_a_number(monkeypatch: pytest.MonkeyPatch) -> None:
    check = check_copy(CV, cf.sources_for(CONTACT, None), BlindConfig(), swaps()["name"])

    async def down(*args: Any, **kwargs: Any) -> str:
        return "score_model_unavailable:budget"

    monkeypatch.setattr("pairly_hire.screening.pipeline.score_cv", down)
    scored = await cf.score_pair(check, object(), object(), live=True, org_id=APP)  # type: ignore[arg-type]
    assert scored.status == "score_model_unavailable:budget"
    assert scored.delta is None


def test_mode_follows_the_router() -> None:
    assert model_mode(GraphServices()) == "recorded"
    recorded = SimpleNamespace(mode=ModelMode.RECORDED)
    live = SimpleNamespace(mode=ModelMode.LIVE)
    assert model_mode(GraphServices(router=recorded)) == "recorded"  # type: ignore[arg-type]
    assert model_mode(GraphServices(router=live)) == "live"  # type: ignore[arg-type]


async def test_the_control_is_scored_once_per_cv(monkeypatch: pytest.MonkeyPatch) -> None:
    check = check_copy(CV, cf.sources_for(CONTACT, None), BlindConfig(), swaps()["name"])
    calls: list[Any] = []

    async def fake_score(*args: Any, **kwargs: Any) -> Any:
        calls.append(args)
        return SimpleNamespace(result=SimpleNamespace(overall=70.0))

    monkeypatch.setattr("pairly_hire.screening.pipeline.score_cv", fake_score)
    scored = await cf.score_pair(
        check,
        object(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        live=True,
        org_id=APP,
        control_score=70.0,
    )
    assert (scored.delta, len(calls)) == (0.0, 1)
