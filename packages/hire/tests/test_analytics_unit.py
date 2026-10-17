# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Hiring analytics on a small hand-built dataset whose every answer is known."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from pairly_hire.analytics.facts import (
    AppFact,
    ApprovalStepFact,
    Facts,
    InterviewFact,
    Move,
    ScreeningFact,
    effective_moves,
)
from pairly_hire.analytics.metrics import Analytics, Metric, Settings, compute, stays

T0 = datetime(2026, 9, 1, 9, 0, tzinfo=UTC)
NOW = T0 + timedelta(days=29)
U1 = uuid.UUID(int=1)
U2 = uuid.UUID(int=2)
JOB = uuid.UUID(int=100)


def d(days: float) -> datetime:
    return T0 + timedelta(days=days)


def h(hours: float) -> datetime:
    return T0 + timedelta(hours=hours)


def screening(
    status: str,
    overall: float | None,
    *,
    flags: tuple[str, ...] = (),
    reasons: tuple[str, ...] = (),
) -> ScreeningFact:
    return ScreeningFact(uuid.uuid4(), status, overall, flags, reasons)


def app(n: int, **kw: object) -> AppFact:
    base: dict[str, object] = {
        "id": uuid.UUID(int=n),
        "job_id": JOB,
        "reference": f"APP{n}-0000",
        "source": "careers",
        "submitted_at": T0,
        "status": "received",
        "stage": "applied",
        "last_update_at": None,
    }
    return AppFact(**(base | kw))  # type: ignore[arg-type]


A1 = app(
    1,
    stage="hired",
    first_response_at=h(4),
    last_update_at=h(4),
    moves=(
        Move("applied", "screening", d(1)),
        Move("screening", "shortlist", d(3)),
        Move("shortlist", "interview", d(5)),
        Move("interview", "offer", d(10)),
        Move("offer", "hired", d(12)),
    ),
    screening=screening("shortlisted", 80.0),
    overrides=1,
    cost_micros=2000,
    model_calls=2,
)
A2 = app(
    2,
    source="referral",
    status="rejected",
    stage="rejected",
    first_response_at=h(10),
    last_update_at=h(10),
    moves=(Move("applied", "screening", d(2)), Move("screening", "rejected", d(4))),
    screening=screening(
        "not_shortlisted", 70.0, flags=("overlapping_roles",), reasons=("guardrails_flagged:x",)
    ),
    cost_micros=1000,
    model_calls=1,
)
A3 = app(3, submitted_at=d(20))
A4 = app(
    4,
    submitted_at=d(25),
    first_response_at=d(25) + timedelta(hours=2),
    last_update_at=d(25) + timedelta(hours=2),
    screening=screening("not_shortlisted", 50.0),
    model_calls=1,
    recorded_calls=1,
)
A5 = app(
    5,
    source="referral",
    submitted_at=d(1),
    stage="screening",
    moves=(Move("applied", "screening", d(2)),),
    screening=screening("awaiting_review", 40.0, flags=("boilerplate",)),
)

INTERVIEWS = (
    InterviewFact(uuid.uuid4(), A1.id, JOB, "completed", d(5), (U1, U2), True),
    InterviewFact(uuid.uuid4(), A1.id, JOB, "completed", d(8), (U1,), False),
    InterviewFact(uuid.uuid4(), A2.id, JOB, "cancelled", d(3), (U2,), False),
    InterviewFact(uuid.uuid4(), A4.id, JOB, "booked", d(30), (U2,), True),
)
A_ID = uuid.uuid4()
STEPS = (
    ApprovalStepFact(A_ID, "hire.offer", "hr", "approved", d(2), d(3)),
    ApprovalStepFact(uuid.uuid4(), "hire.offer", "hr", "approved", d(2), d(1)),
    ApprovalStepFact(uuid.uuid4(), "hire.message", "reviewer", "active", d(28), None),
    ApprovalStepFact(uuid.uuid4(), "hire.message", "reviewer", "waiting", None, None),
)
FACTS = Facts(
    apps=(A1, A2, A3, A4, A5),
    interviews=INTERVIEWS,
    steps=STEPS,
    people={U1: "Interviewer One", U2: "Interviewer Two"},
)
SETTINGS = Settings(sla_days=7, ghost_days=10, agreement_threshold=60, minutes_per_booking=30)


@pytest.fixture(scope="module")
def result() -> Analytics:
    return compute(FACTS, now=NOW, settings=SETTINGS)


def refs(m: Metric) -> set[str]:
    return {r.label for r in m.records}


def test_time_to_first_response(result: Analytics) -> None:
    m = result.time_to_first_response
    assert (m.value, m.n, m.detail["mean"], m.detail["pending"]) == (4.0, 3, 5.33, 2)
    assert refs(m) == {"APP1-0000", "APP2-0000", "APP4-0000"}


def test_time_in_stage_and_time_to_hire(result: Analytics) -> None:
    by = {m.detail["stage"]: m for m in result.time_in_stage}
    assert (by["applied"].value, by["applied"].n, by["applied"].detail["mean"]) == (1.0, 3, 1.33)
    assert (by["screening"].value, by["screening"].n) == (2.0, 2)
    assert (by["shortlist"].value, by["interview"].value, by["offer"].value) == (2.0, 5.0, 2.0)
    assert result.time_to_hire.value == 12.0
    assert refs(result.time_to_hire) == {"APP1-0000"}
    # An open stay is not counted.
    assert stays(A5) == [("applied", 1.0)]


def test_stage_conversion(result: Analytics) -> None:
    by = {m.detail["stage"]: m for m in result.stage_conversion}
    assert (by["applied"].detail["reached"], by["applied"].value) == (5, 0.6)
    assert (by["screening"].detail["reached"], by["screening"].value) == (3, 0.3333)
    assert by["shortlist"].value == 1.0
    assert by["offer"].detail["next_stage"] == "hired"
    assert refs(by["screening"]) == {"APP1-0000"}


def test_sources(result: Analytics) -> None:
    by = {m.detail["source"]: m for m in result.sources}
    careers = by["careers"].detail
    assert (careers["applications"], careers["shortlisted"], careers["hired"]) == (3, 1, 1)
    assert by["careers"].value == 0.3333
    assert by["referral"].value == 0.0
    assert by["referral"].detail["shortlist_rate"] == 0.0


def test_sla_and_ghosting(result: Analytics) -> None:
    ghosting, approvals = result.sla
    assert (ghosting.value, ghosting.n) == (2.0, 3)
    assert refs(ghosting) == {"APP3-0000", "APP5-0000"}
    assert (approvals.value, approvals.n) == (2.0, 3)
    assert {r.kind for r in approvals.records} == {"approval"}
    assert str(A_ID) in {r.id for r in approvals.records}
    g = result.ghosting_rate
    assert (g.value, g.n, g.detail["ghosted"]) == (0.3333, 3, 1)
    assert refs(g) == {"APP5-0000"}


def test_interviewer_load_and_time_saved(result: Analytics) -> None:
    load = {m.detail["user_id"]: m for m in result.interviewer_load}
    assert (load[str(U1)].value, load[str(U1)].detail["upcoming"]) == (2.0, 0)
    assert (load[str(U2)].value, load[str(U2)].detail["upcoming"]) == (2.0, 1)
    assert load[str(U1)].detail["name"] == "Interviewer One"
    saved = result.scheduling_time_saved
    assert (saved.value, saved.n, saved.detail["estimate"]) == (60.0, 2, True)


def test_agreement_override_flags_and_cost(result: Analytics) -> None:
    agreement, disagreement = result.agreement
    assert (agreement.value, agreement.n) == (0.6667, 3)
    assert refs(disagreement) == {"APP2-0000"}
    assert (result.override_rate.value, result.override_rate.n) == (0.25, 4)
    assert refs(result.override_rate) == {"APP1-0000"}
    injection, claims, signals = result.flags
    assert (injection.value, claims.value, signals.value) == (1.0, 1.0, 1.0)
    assert refs(injection) == refs(claims) == {"APP2-0000"}
    assert refs(signals) == {"APP5-0000"}
    cost = result.cost_per_screened_application
    assert (cost.value, cost.n, cost.detail["model_calls"]) == (0.00075, 4, 4)
    assert cost.detail["recorded_mode"] is False


def test_recorded_mode_costs_zero_and_says_so() -> None:
    only = Facts(apps=(A4,))
    cost = compute(only, now=NOW, settings=SETTINGS).cost_per_screened_application
    assert (cost.value, cost.detail["recorded_mode"]) == (0.0, True)


def test_no_data_is_none_never_zero() -> None:
    empty = compute(Facts(apps=()), now=NOW, settings=SETTINGS)
    assert empty.time_to_first_response.value is None
    assert empty.time_to_hire.value is None
    assert empty.ghosting_rate.value is None
    assert empty.override_rate.value is None
    assert empty.cost_per_screened_application.value is None
    assert empty.agreement[0].value is None


def test_undone_moves_are_left_out() -> None:
    m1, m2, m3 = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    moves = effective_moves(
        [
            (m1, None, Move("applied", "screening", d(1))),
            (m2, m1, Move("screening", "applied", d(1.5))),
            (m3, None, Move("applied", "shortlist", d(2))),
        ]
    )
    assert moves == [Move("applied", "shortlist", d(2))]
