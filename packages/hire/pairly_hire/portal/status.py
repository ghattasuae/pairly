# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""What a candidate sees of where their application stands (brief 3.9.1).

Honest, human-readable steps, mapped from the job's internal pipeline stages. The
portal never shows a stage's internal key or label (a job may name a stage anything),
scores, notes, tags, flags, reasons or who moved the application: only one of these
steps and when it was reached.

=================  ==========================================================
``received``       the application arrived (the first stage)
``in_review``      a person is reviewing it (any active stage that does not
                   reveal identity, such as screening)
``moving_forward`` it passed the first review (an active stage that reveals
                   identity, such as the shortlist)
``interview``      the ``interview`` stage
``offer``          the ``offer`` stage
``hired``          the hired outcome
``not_selected``   the rejected outcome, once the rejection message was sent
``withdrawn``      the candidate withdrew it
=================  ==========================================================

A rejection waits for a person to approve the message that tells the candidate
(``messaging.graph``); until that message is sent the candidate still sees the step
they were in, so the portal never announces a decision before it was communicated.
Undone moves never show (an undo reverts a slip, not the candidate's progress).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from pairly_hire.pipeline.stages import FIRST, Stage, StageSet


class Step(StrEnum):
    RECEIVED = "received"
    IN_REVIEW = "in_review"
    MOVING_FORWARD = "moving_forward"
    INTERVIEW = "interview"
    OFFER = "offer"
    HIRED = "hired"
    NOT_SELECTED = "not_selected"
    WITHDRAWN = "withdrawn"


FINAL = frozenset({Step.HIRED, Step.NOT_SELECTED, Step.WITHDRAWN})


def step_for(stage: Stage, *, rejection_sent: bool) -> Step | None:
    """The candidate-facing step of one internal stage (None: not shown yet)."""
    if stage.kind == "hired":
        return Step.HIRED
    if stage.kind == "withdrawn":
        return Step.WITHDRAWN
    if stage.kind == "rejected":
        return Step.NOT_SELECTED if rejection_sent else None
    if stage.key == FIRST:
        return Step.RECEIVED
    if stage.key == "interview":
        return Step.INTERVIEW
    if stage.key == "offer":
        return Step.OFFER
    return Step.MOVING_FORWARD if stage.reveal_identity else Step.IN_REVIEW


@dataclass(frozen=True, slots=True)
class MoveFact:
    """One stage move, as the timeline needs it (no actor, no reason)."""

    id: uuid.UUID
    at: datetime
    to_stage: str
    undo_of: uuid.UUID | None


@dataclass(frozen=True, slots=True)
class TimelineEntry:
    step: Step
    at: datetime


@dataclass(frozen=True, slots=True)
class Timeline:
    current: Step
    since: datetime
    history: tuple[TimelineEntry, ...]

    @property
    def final(self) -> bool:
        return self.current in FINAL


def timeline(
    stages: StageSet,
    submitted_at: datetime,
    moves: Sequence[MoveFact],
    *,
    rejection_sent_at: datetime | None,
) -> Timeline:
    """The candidate's timeline from the stage moves (see the module docstring)."""
    undone = {m.undo_of for m in moves if m.undo_of is not None}
    history: list[TimelineEntry] = [TimelineEntry(Step.RECEIVED, submitted_at)]
    for move in sorted(moves, key=lambda m: m.at):
        if move.undo_of is not None or move.id in undone:
            continue
        stage = stages.get(move.to_stage)
        if stage is None:
            continue
        sent = rejection_sent_at is not None
        step = step_for(stage, rejection_sent=sent)
        if step is None:
            continue
        at = move.at
        if step is Step.NOT_SELECTED and rejection_sent_at is not None:
            at = max(move.at, rejection_sent_at)
        if history[-1].step is step:
            continue
        history.append(TimelineEntry(step, at))
    last = history[-1]
    return Timeline(last.step, last.at, tuple(history))
