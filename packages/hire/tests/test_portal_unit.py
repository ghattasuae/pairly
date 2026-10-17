# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Candidate portal, pure parts: the honest timeline, token parsing, the candidate actor."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from pairly_core.rbac import ActorKind, candidate_may_hold
from pairly_hire.pipeline.stages import DEFAULT_STAGES, validate_stage_set
from pairly_hire.portal import auth
from pairly_hire.portal.status import MoveFact, Step, step_for, timeline

T0 = datetime(2026, 10, 1, 9, tzinfo=UTC)


def _move(stage: str, minutes: int, undo_of: uuid.UUID | None = None) -> MoveFact:
    return MoveFact(uuid.uuid4(), T0 + timedelta(minutes=minutes), stage, undo_of)


def test_default_stages_map_to_honest_steps() -> None:
    steps = {s.key: step_for(s, rejection_sent=False) for s in DEFAULT_STAGES.stages}
    assert steps == {
        "applied": Step.RECEIVED,
        "screening": Step.IN_REVIEW,
        "shortlist": Step.MOVING_FORWARD,
        "interview": Step.INTERVIEW,
        "offer": Step.OFFER,
        "hired": Step.HIRED,
        "rejected": None,
        "withdrawn": Step.WITHDRAWN,
    }
    rejected = DEFAULT_STAGES.require("rejected")
    assert step_for(rejected, rejection_sent=True) is Step.NOT_SELECTED


def test_custom_stages_never_leak_their_names() -> None:
    stages = validate_stage_set(
        [
            {"key": "applied", "label": "New"},
            {"key": "phone_screen", "label": "Weak, check"},
            {"key": "case_study", "label": "Top", "reveal_identity": True},
            {"key": "hired", "label": "Hired", "kind": "hired"},
            {"key": "rejected", "label": "No", "kind": "rejected"},
            {"key": "withdrawn", "label": "Gone", "kind": "withdrawn"},
        ]
    )
    line = timeline(
        stages, T0, [_move("phone_screen", 5), _move("case_study", 10)], rejection_sent_at=None
    )
    assert [e.step for e in line.history] == [Step.RECEIVED, Step.IN_REVIEW, Step.MOVING_FORWARD]
    assert line.current is Step.MOVING_FORWARD


def test_undone_moves_and_unsent_rejections_never_show() -> None:
    slip = _move("offer", 10)
    moves = [
        _move("screening", 5),
        slip,
        _move("screening", 11, undo_of=slip.id),
        _move("rejected", 20),
    ]
    line = timeline(DEFAULT_STAGES, T0, moves, rejection_sent_at=None)
    assert [e.step for e in line.history] == [Step.RECEIVED, Step.IN_REVIEW]
    assert not line.final
    sent = T0 + timedelta(hours=2)
    line = timeline(DEFAULT_STAGES, T0, moves, rejection_sent_at=sent)
    assert line.current is Step.NOT_SELECTED
    assert line.since == sent
    assert line.final


def test_tokens_parse_strictly() -> None:
    org, row, cand = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    secret = "A" * 43
    good = f"pl1.{org.hex}.{row.hex}.{cand.hex}.{secret}"
    claim = auth.parse(auth.LOGIN_PREFIX, good)
    assert claim is not None
    assert (claim.org_id, claim.row_id, claim.candidate_id) == (org, row, cand)
    for bad in (
        good.replace("pl1", "ps1", 1),
        good + "x",
        f"pl1.{org.hex}.{row.hex}.{cand.hex}.{'A' * 42}!",
        f"pl1.{org.hex}.{row.hex}.{cand.hex}",
        "pl1." + "z" * 32 + f".{row.hex}.{cand.hex}.{secret}",
        "",
    ):
        assert auth.parse(auth.LOGIN_PREFIX, bad) is None, bad
    assert auth.parse(auth.SESSION_PREFIX, good) is None


def test_the_candidate_actor_holds_only_portal_rights() -> None:
    identity = auth.PortalIdentity(uuid.uuid4(), uuid.uuid4(), uuid.uuid4())
    actor = identity.actor()
    assert actor.kind is ActorKind.CANDIDATE
    assert actor.permissions == {"hire.portal.read", "hire.appeal.create"}
    assert candidate_may_hold("hire.portal.read")
    assert candidate_may_hold("hire.appeal.create")
    for staff in (
        "hire.candidate.read",
        "hire.appeal.decide",
        "core.appeal.create",
        "hire.note.write",
    ):
        assert not candidate_may_hold(staff), staff
