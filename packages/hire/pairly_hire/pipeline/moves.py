# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Stage moves, undo and bulk actions (brief 3.4.1 and 5.3: undo instead of confirm).

``move`` puts one application in another stage of its job's pipeline:

* a person only (``hire.candidate.decide``, a second factor sign-in), never an agent;
* one row in ``hire_stage_moves`` and one audit event (``hire.candidate.moved``) per
  move, in the same transaction;
* a move into a stage that reveals identity sets the application's one reveal state
  (``pairly_hire.reveal``, audited); an undo never clears it;
* the application's status follows the outcome stages: ``rejected`` (with
  ``rejected_at`` and the retention date of ``pairly_hire.retention``), ``withdrawn``,
  and back to ``received`` when it leaves them;
* moving to ``rejected`` never sends anything. It drafts a rejection in the tone of the
  stage the candidate was in (``library.rejection_template_for``), which a person
  submits for approval and another person approves before it is sent.

``undo`` reverts moves the same person made in the last ``UNDO_SECONDS`` (the web shows
a 10 second toast; the server allows a little more for slow networks), as long as the
application has not moved since. Undoing a rejection cancels its draft; once the draft
was submitted for approval the move cannot be undone here (the approval is the place
to stop it). An undo is a new move pointing at the one it reverts.

``withdraw`` is the candidate's own move to ``withdrawn`` from the portal
(``ActorKind.CANDIDATE``, only their own application, never after an outcome other than
an unannounced rejection): it cancels the open drafts of the application, and a
rejection still waiting for approval is never sent (``messaging.graph`` refuses to send
to a withdrawn application).

``bulk_summary`` says what a bulk action would do before anything happens: how many
applications move, from which stages, how many drafts it would create, and that
nothing is sent. Bulk moves share a ``batch_id`` so one undo reverts them all.
"""

from __future__ import annotations

import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from pairly_core.audit import actor_ref, audit_actor_action
from pairly_core.crypto.types import seal
from pairly_core.errors import BadRequestError, ConflictError, ForbiddenError, NotFoundError
from pairly_core.rbac import Actor, ActorKind, authorize
from pairly_hire.jobs import org_settings
from pairly_hire.links import LinkConfig
from pairly_hire.messaging import library
from pairly_hire.messaging.compose import Author, cancel_draft, create_draft
from pairly_hire.messaging.models import HireMessage, MessagePurpose, MessageStatus
from pairly_hire.models import ApplicationStatus, HireApplication
from pairly_hire.permissions import CANDIDATE_DECIDE
from pairly_hire.pipeline.models import HireStageMove
from pairly_hire.pipeline.records import AppRecord, ensure_entry, load_record
from pairly_hire.pipeline.stages import HIRED, REJECTED, WITHDRAWN, Stage
from pairly_hire.retention import retention_until
from pairly_hire.reveal import reveal_identity, stage_source

WORKFLOW: Final = "hire"
ENTITY: Final = "hire.application"
MOVED: Final = "hire.candidate.moved"
UNDONE: Final = "hire.candidate.move_undone"
UNDO_SECONDS: Final = 60
MAX_BULK: Final = 200
MAX_REASON: Final = 500


@dataclass(frozen=True, slots=True)
class MoveResult:
    move_id: uuid.UUID
    application_id: uuid.UUID
    from_stage: str
    to_stage: str
    # The rejection draft a move to ``rejected`` created (a person submits it).
    draft_id: uuid.UUID | None = None


def _person(actor: Actor) -> None:
    authorize(actor, CANDIDATE_DECIDE)
    if actor.kind is not ActorKind.USER:
        raise ForbiddenError("only a person can move a candidate")


def _apply_status(app: HireApplication, to: Stage, now: datetime, months: int) -> None:
    if to.key == REJECTED:
        app.status = ApplicationStatus.REJECTED.value
        app.rejected_at = now
        app.retention_until = retention_until(now, months)
    elif to.key == WITHDRAWN:
        app.status = ApplicationStatus.WITHDRAWN.value
        app.rejected_at = None
        app.retention_until = None
    else:
        app.status = ApplicationStatus.RECEIVED.value
        app.rejected_at = None
        app.retention_until = None
    app.updated_at = now


async def _record_move(
    session: AsyncSession,
    actor: Actor,
    record: AppRecord,
    to: Stage,
    *,
    reason: str | None,
    batch_id: uuid.UUID | None,
    undo_of: uuid.UUID | None,
) -> HireStageMove:
    now = datetime.now(UTC)
    from_stage = record.stage_key
    entry = await ensure_entry(session, record)
    entry.stage_key = to.key
    entry.stage_entered_at = now
    entry.updated_at = now
    months = (await org_settings(session)).rejected_retention_months
    _apply_status(record.app, to, now, months)
    kind, actor_id = actor_ref(actor)
    move = HireStageMove(
        id=uuid.uuid4(),
        org_id=record.app.org_id,
        application_id=record.app.id,
        job_id=record.app.job_id,
        from_stage=from_stage,
        to_stage=to.key,
        actor_type=kind.value,
        actor_id=actor_id,
        batch_id=batch_id,
        undo_of=undo_of,
        created_at=now,
    )
    if reason:
        sealed = seal(
            HireStageMove, "reason", reason[:MAX_REASON], org_id=move.org_id, row_id=move.id
        )
        setattr(move, "reason", sealed)  # noqa: B010 (typed column, sealed value)
    session.add(move)
    await session.flush()
    await audit_actor_action(
        session,
        actor,
        UNDONE if undo_of else MOVED,
        entity_type=ENTITY,
        entity_id=record.app.id,
        workflow=WORKFLOW,
        payload={
            "job_id": str(record.app.job_id),
            "from": from_stage,
            "to": to.key,
            "move_id": str(move.id),
            "batch_id": str(batch_id) if batch_id else None,
            "undo_of": str(undo_of) if undo_of else None,
            "with_reason": bool(reason),
        },
    )
    if to.reveal_identity:
        # The one reveal state (pairly_hire.reveal), set once. An undo never hides it.
        await reveal_identity(
            session,
            record.app,
            source=stage_source(to.key),
            actor_type=kind,
            actor_id=actor_id,
            payload={"move_id": str(move.id)},
        )
    return move


async def _pending_rejection(
    session: AsyncSession, application_id: uuid.UUID
) -> HireMessage | None:
    return await session.scalar(
        select(HireMessage)
        .where(
            HireMessage.application_id == application_id,
            HireMessage.purpose == MessagePurpose.REJECTION.value,
            HireMessage.status.in_(
                [
                    MessageStatus.DRAFT.value,
                    MessageStatus.CHANGES_REQUESTED.value,
                    MessageStatus.SUBMITTED.value,
                    MessageStatus.AWAITING_APPROVAL.value,
                ]
            ),
        )
        .order_by(HireMessage.created_at.desc())
        .limit(1)
    )


async def move(
    session: AsyncSession,
    actor: Actor,
    application_id: uuid.UUID,
    to_stage: str,
    *,
    links: LinkConfig,
    reason: str | None = None,
    batch_id: uuid.UUID | None = None,
) -> MoveResult:
    """Move one application (see the module docstring)."""
    _person(actor)
    record = await load_record(session, application_id, lock=True)
    to = record.stages.require(to_stage)
    from_stage = record.stage_key
    if to.key == from_stage:
        raise ConflictError(f"the candidate is already in {to.label}")
    moved = await _record_move(
        session, actor, record, to, reason=reason, batch_id=batch_id, undo_of=None
    )
    draft_id: uuid.UUID | None = None
    if to.key == REJECTED:
        existing = await _pending_rejection(session, record.app.id)
        if existing is None:
            kind, actor_id = actor_ref(actor)
            draft = await create_draft(
                session,
                record,
                Author(kind, actor_id),
                template_key=library.rejection_template_for(from_stage),
                purpose=MessagePurpose.REJECTION,
                links=links,
            )
            draft_id = draft.id
        else:
            draft_id = existing.id
    return MoveResult(moved.id, record.app.id, from_stage, to.key, draft_id)


async def move_next(
    session: AsyncSession, actor: Actor, application_id: uuid.UUID, *, links: LinkConfig
) -> MoveResult:
    """Advance to the next active stage (``hired`` after the last one)."""
    _person(actor)
    record = await load_record(session, application_id, lock=True)
    following = record.stages.next_after(record.stage_key)
    if following is None:
        raise ConflictError("the candidate is already at the end of the pipeline")
    return await move(session, actor, application_id, following.key, links=links)


@dataclass(frozen=True, slots=True)
class WithdrawResult:
    move: MoveResult
    cancelled_drafts: list[uuid.UUID]


async def withdraw(
    session: AsyncSession, actor: Actor, application_id: uuid.UUID
) -> WithdrawResult:
    """The candidate withdraws their own application (see the module docstring)."""
    if actor.kind is not ActorKind.CANDIDATE:
        raise ForbiddenError("only the candidate can withdraw their application")
    record = await load_record(session, application_id, lock=True)
    if record.candidate.id != actor.id:
        raise NotFoundError("application not found")
    if record.app.status == ApplicationStatus.WITHDRAWN.value:
        raise ConflictError("this application was already withdrawn")
    if record.stage.kind == "hired" or record.stage_key == HIRED:
        raise ConflictError("this application has an outcome; contact the hiring team")
    if record.stage.kind == "rejected" and await _rejection_sent(session, record.app.id):
        raise ConflictError("this application has an outcome; contact the hiring team")
    to = record.stages.require(WITHDRAWN)
    from_stage = record.stage_key
    moved = await _record_move(session, actor, record, to, reason=None, batch_id=None, undo_of=None)
    kind, actor_id = actor_ref(actor)
    cancelled: list[uuid.UUID] = []
    for draft in await session.scalars(
        select(HireMessage)
        .where(
            HireMessage.application_id == record.app.id,
            HireMessage.status.in_(
                [MessageStatus.DRAFT.value, MessageStatus.CHANGES_REQUESTED.value]
            ),
        )
        .with_for_update()
    ):
        await cancel_draft(session, draft, Author(kind, actor_id))
        cancelled.append(draft.id)
    return WithdrawResult(MoveResult(moved.id, record.app.id, from_stage, to.key), cancelled)


async def _rejection_sent(session: AsyncSession, application_id: uuid.UUID) -> bool:
    found = await session.scalar(
        select(HireMessage.id).where(
            HireMessage.application_id == application_id,
            HireMessage.purpose == MessagePurpose.REJECTION.value,
            HireMessage.status.in_([MessageStatus.QUEUED.value, MessageStatus.SENT.value]),
        )
    )
    return found is not None


@dataclass(frozen=True, slots=True)
class UndoResult:
    undone: list[MoveResult]
    cancelled_drafts: list[uuid.UUID] = field(default_factory=list)


async def undo(session: AsyncSession, actor: Actor, move_ids: list[uuid.UUID]) -> UndoResult:
    """Revert the actor's own recent moves (see the module docstring). All or nothing."""
    _person(actor)
    if not move_ids or len(move_ids) > MAX_BULK:
        raise BadRequestError(f"undo 1 to {MAX_BULK} moves")
    kind, actor_id = actor_ref(actor)
    moves = list(
        await session.scalars(
            select(HireStageMove)
            .where(HireStageMove.id.in_(move_ids))
            .order_by(HireStageMove.created_at.desc())
        )
    )
    if len(moves) != len(set(move_ids)):
        raise NotFoundError("move not found")
    now = datetime.now(UTC)
    undone: list[MoveResult] = []
    cancelled: list[uuid.UUID] = []
    for original in moves:
        if original.actor_id != actor_id or original.actor_type != kind.value:
            raise ForbiddenError("you can only undo your own moves")
        if original.undo_of is not None:
            raise ConflictError("an undo cannot be undone; move the candidate instead")
        if now - original.created_at > timedelta(seconds=UNDO_SECONDS):
            raise ConflictError("too late to undo; move the candidate instead")
        already = await session.scalar(
            select(HireStageMove.id).where(HireStageMove.undo_of == original.id)
        )
        if already is not None:
            raise ConflictError("this move was already undone")
        record = await load_record(session, original.application_id, lock=True)
        if record.stage_key != original.to_stage:
            raise ConflictError("the candidate moved again since; move them instead")
        if original.to_stage == REJECTED:
            draft = await _pending_rejection(session, record.app.id)
            if draft is not None and draft.status not in {
                MessageStatus.DRAFT.value,
                MessageStatus.CHANGES_REQUESTED.value,
            }:
                raise ConflictError(
                    "the rejection is already waiting for approval; stop it there first"
                )
            if draft is not None:
                await cancel_draft(session, draft, Author(kind, actor_id))
                cancelled.append(draft.id)
        back = record.stages.require(original.from_stage)
        reverted = await _record_move(
            session,
            actor,
            record,
            back,
            reason=None,
            batch_id=original.batch_id,
            undo_of=original.id,
        )
        undone.append(MoveResult(reverted.id, record.app.id, original.to_stage, back.key))
    return UndoResult(undone, cancelled)


# ------------------------------------------------------------------ bulk


@dataclass(frozen=True, slots=True)
class BulkSummary:
    to_stage: str
    to_label: str
    total: int
    will_move: int
    already_there: list[uuid.UUID]
    not_found: list[uuid.UUID]
    from_stages: dict[str, int]
    drafts_to_create: int
    # Moves never send anything; drafts wait for a person (and rejections for approval).
    messages_sent_now: int = 0
    needs_approval: bool = False


async def bulk_summary(
    session: AsyncSession,
    actor: Actor,
    job_id: uuid.UUID,
    application_ids: list[uuid.UUID],
    to_stage: str,
) -> BulkSummary:
    _person(actor)
    ids = list(dict.fromkeys(application_ids))
    if not ids or len(ids) > MAX_BULK:
        raise BadRequestError(f"choose 1 to {MAX_BULK} candidates")
    counts: Counter[str] = Counter()
    already: list[uuid.UUID] = []
    missing: list[uuid.UUID] = []
    stage_label = to_stage
    drafts = 0
    for app_id in ids:
        try:
            record = await load_record(session, app_id)
        except NotFoundError:
            missing.append(app_id)
            continue
        if record.app.job_id != job_id:
            missing.append(app_id)
            continue
        to = record.stages.require(to_stage)
        stage_label = to.label
        if record.stage_key == to.key:
            already.append(app_id)
            continue
        counts[record.stage_key] += 1
        if to.key == REJECTED and await _pending_rejection(session, app_id) is None:
            drafts += 1
    will = sum(counts.values())
    return BulkSummary(
        to_stage=to_stage,
        to_label=stage_label,
        total=len(ids),
        will_move=will,
        already_there=already,
        not_found=missing,
        from_stages=dict(counts),
        drafts_to_create=drafts,
        needs_approval=to_stage == REJECTED and will > 0,
    )


async def bulk_move(
    session: AsyncSession,
    actor: Actor,
    job_id: uuid.UUID,
    application_ids: list[uuid.UUID],
    to_stage: str,
    *,
    links: LinkConfig,
) -> tuple[uuid.UUID, list[MoveResult]]:
    """Move many applications of one job at once (one batch, one undo)."""
    summary = await bulk_summary(session, actor, job_id, application_ids, to_stage)
    if summary.not_found:
        raise NotFoundError("some candidates were not found in this job")
    batch = uuid.uuid4()
    results: list[MoveResult] = []
    for app_id in dict.fromkeys(application_ids):
        if app_id in summary.already_there:
            continue
        results.append(await move(session, actor, app_id, to_stage, links=links, batch_id=batch))
    return batch, results
