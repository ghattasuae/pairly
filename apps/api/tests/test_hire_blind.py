# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Blind mode across Pairly Hire, on Postgres, offline (recorded screening).

1. One reveal state per application (``pairly_hire.reveal``): a board move into a stage
   that reveals identity and the shortlist approval set the same flag, once, audited;
   the screening panel, the pipeline, messaging and the application list and detail
   all read it; an undo never hides again.
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine

from synth.hire.screening import contact_for

from .hire_support import HireWorld, owner_rows
from .test_hire_screening import ScreenFactory, screen_factory, spec, world

__all__ = ["screen_factory", "world"]  # fixtures

REVEALED = "hire.candidate.identity_revealed"


async def _reveal_events(owner: AsyncEngine, world: HireWorld, app_id: str) -> list[Any]:
    return await owner_rows(
        owner,
        world.org_id,
        "SELECT actor_type, payload FROM audit_events WHERE action = :a AND entity_id = :e",
        {"a": REVEALED, "e": app_id},
    )


async def test_one_reveal_state_for_board_moves_and_shortlist(
    screen_factory: ScreenFactory, world: HireWorld, owner_engine: AsyncEngine
) -> None:
    api = await screen_factory()
    job = await api.open_job(world)
    app_id = await api.apply(world, job, "cv-0026")
    contact = contact_for(spec("cv-0026"))
    view = await api.screening(world, app_id)
    assert view["status"] == "awaiting_review"
    assert view["blind"] is True
    assert view["document_id"] is None

    async def get(path: str) -> dict[str, Any]:
        response = await api.call("GET", path, world, "recruiter")
        assert response.status_code == 200, (path, response.text)
        return dict(response.json())

    # A board move into the shortlist stage (no screening approval yet) reveals the
    # application everywhere, the screening panel included.
    moved = await api.call(
        "POST",
        f"/v1/hire/pipeline/applications/{app_id}/move",
        world,
        "recruiter",
        json={"to_stage": "shortlist"},
    )
    assert moved.status_code == 200, moved.text

    async def check_revealed() -> None:
        shown = await api.screening(world, app_id)
        assert shown["blind"] is False
        assert shown["document_id"] is not None
        assert contact["full_name"] in (shown["text"] or "")
        assert (await get(f"/v1/hire/applications/{app_id}"))["email"] == contact["email"]
        listed = await get(f"/v1/hire/applications?job_id={job['id']}")
        assert listed["items"][0]["candidate_name"] == contact["full_name"]
        profile = await get(f"/v1/hire/pipeline/applications/{app_id}")
        assert profile["identity_hidden"] is False
        assert profile["contact"]["email"] == contact["email"]
        card = (await get(f"/v1/hire/jobs/{job['id']}/pipeline"))["cards"][0]
        assert card["label"] == contact["full_name"]

    await check_revealed()
    events = await _reveal_events(owner_engine, world, app_id)
    assert len(events) == 1
    assert events[0].payload["source"] == "stage:shortlist"
    assert events[0].actor_type == "user"

    # The undo puts the candidate back, but what a person saw stays seen.
    undone = await api.call(
        "POST",
        "/v1/hire/pipeline/moves/undo",
        world,
        "recruiter",
        json={"move_ids": [moved.json()["move_id"]]},
    )
    assert undone.status_code == 200, undone.text
    await check_revealed()

    # The shortlist approval later finds it revealed: no second reveal event.
    decided = await api.decide(world, view["approval_id"], "approve")
    assert decided.status_code == 200, decided.text
    assert (await api.screening(world, app_id))["status"] == "shortlisted"
    assert len(await _reveal_events(owner_engine, world, app_id)) == 1


async def test_shortlist_approval_reveals_the_pipeline_without_a_move(
    screen_factory: ScreenFactory, world: HireWorld, owner_engine: AsyncEngine
) -> None:
    api = await screen_factory()
    job = await api.open_job(world)
    app_id = await api.apply(world, job, "cv-0029")
    contact = contact_for(spec("cv-0029"))
    view = await api.screening(world, app_id)
    board = await api.call("GET", f"/v1/hire/jobs/{job['id']}/pipeline", world, "recruiter")
    assert contact["full_name"] not in json.dumps(board.json())
    decided = await api.decide(world, view["approval_id"], "approve")
    assert decided.status_code == 200, decided.text
    board = await api.call("GET", f"/v1/hire/jobs/{job['id']}/pipeline", world, "recruiter")
    (card,) = board.json()["cards"]
    # Still in the first stage: the reveal is the application's, not the stage's.
    assert card["stage_key"] == "applied"
    assert card["label"] == contact["full_name"]
    events = await _reveal_events(owner_engine, world, app_id)
    assert [e.payload["source"] for e in events] == ["shortlist"]
    assert events[0].actor_type == "system"
    (row,) = await owner_rows(
        owner_engine,
        world.org_id,
        "SELECT identity_revealed_by FROM hire_applications WHERE id = :i",
        {"i": app_id},
    )
    assert row.identity_revealed_by == "shortlist"
