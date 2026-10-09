# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Pipeline stages per job (brief 3.4.1, customisable).

Default stages, in board order::

    applied, screening, shortlist, interview, offer, hired, rejected, withdrawn

A stage has a ``kind``: ``active`` (the candidate is still in the process), or one of
the three outcomes ``hired``, ``rejected``, ``withdrawn``. ``reveal_identity`` marks the
stages where blind mode shows who the candidate is (shortlist and later by default).

A job may customise its stages (``save_stages``): rename them (English and Arabic),
add, remove or reorder active stages. The rules that keep moves meaningful:

* ``applied`` is always first (new applications land there);
* ``hired``, ``rejected`` and ``withdrawn`` always exist, with those kinds;
* keys are lowercase identifiers, unique; at most ``MAX_STAGES`` stages;
* a stage that still holds applications cannot be removed.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from pairly_core.audit import audit_actor_action
from pairly_core.errors import BadRequestError, ConflictError
from pairly_core.rbac import Actor
from pairly_hire.models import HireJob
from pairly_hire.pipeline.models import STAGE_KEY_PATTERN, HirePipelineConfig, HirePipelineEntry

WORKFLOW: Final = "hire"
MAX_STAGES: Final = 16
FIRST: Final = "applied"
HIRED: Final = "hired"
REJECTED: Final = "rejected"
WITHDRAWN: Final = "withdrawn"
SHORTLIST: Final = "shortlist"
OUTCOMES: Final = frozenset({HIRED, REJECTED, WITHDRAWN})

StageKind = Literal["active", "hired", "rejected", "withdrawn"]


class Stage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    key: Annotated[str, StringConstraints(pattern=STAGE_KEY_PATTERN)]
    label: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=60)]
    label_ar: Annotated[str, StringConstraints(strip_whitespace=True, max_length=60)] = ""
    kind: StageKind = "active"
    reveal_identity: bool = False

    @property
    def terminal(self) -> bool:
        return self.kind != "active"


class StageSet(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    stages: list[Stage] = Field(min_length=4, max_length=MAX_STAGES)

    def keys(self) -> list[str]:
        return [s.key for s in self.stages]

    def get(self, key: str) -> Stage | None:
        return next((s for s in self.stages if s.key == key), None)

    def require(self, key: str) -> Stage:
        stage = self.get(key)
        if stage is None:
            raise BadRequestError(f"no stage {key!r} in this job's pipeline")
        return stage

    def next_after(self, key: str) -> Stage | None:
        """The next active stage, or ``hired`` after the last active one. None at the end."""
        active = [s for s in self.stages if s.kind == "active"]
        keys = [s.key for s in active]
        if key not in keys:
            return None
        index = keys.index(key)
        if index + 1 < len(active):
            return active[index + 1]
        return self.get(HIRED)

    def position(self, key: str) -> int:
        keys = self.keys()
        return keys.index(key) if key in keys else -1


DEFAULT_STAGES: Final = StageSet(
    stages=[
        Stage(key=FIRST, label="Applied", label_ar="تقدم"),
        Stage(key="screening", label="Screening", label_ar="الفرز"),
        Stage(key=SHORTLIST, label="Shortlist", label_ar="القائمة المختصرة", reveal_identity=True),
        Stage(key="interview", label="Interview", label_ar="المقابلة", reveal_identity=True),
        Stage(key="offer", label="Offer", label_ar="العرض", reveal_identity=True),
        Stage(key=HIRED, label="Hired", label_ar="تم التوظيف", kind="hired", reveal_identity=True),
        Stage(key=REJECTED, label="Rejected", label_ar="مرفوض", kind="rejected"),
        Stage(key=WITHDRAWN, label="Withdrawn", label_ar="انسحب", kind="withdrawn"),
    ]
)


def validate_stage_set(raw: Any) -> StageSet:
    """Parse and check a stage list (see the module docstring). Raises BadRequestError."""
    try:
        stages = StageSet.model_validate(raw if isinstance(raw, dict) else {"stages": raw})
    except ValidationError as exc:
        errors = exc.errors()
        where = ".".join(str(p) for p in errors[0]["loc"]) if errors else ""
        raise BadRequestError(f"invalid stages: {where}: {errors[0]['msg']}") from None
    keys = stages.keys()
    if len(set(keys)) != len(keys):
        raise BadRequestError("stage keys must be unique")
    if keys[0] != FIRST or stages.stages[0].kind != "active":
        raise BadRequestError("the first stage is always 'applied'")
    for key in OUTCOMES:
        stage = stages.get(key)
        if stage is None or stage.kind != key:
            raise BadRequestError(f"the pipeline needs the '{key}' stage")
    extra_outcomes = [s.key for s in stages.stages if s.kind != "active" and s.key not in OUTCOMES]
    if extra_outcomes:
        raise BadRequestError("only hired, rejected and withdrawn are outcome stages")
    return stages


async def stages_for(session: AsyncSession, job_id: uuid.UUID) -> StageSet:
    row = await session.scalar(
        select(HirePipelineConfig.stages).where(HirePipelineConfig.job_id == job_id)
    )
    if row is None:
        return DEFAULT_STAGES
    return validate_stage_set(row)


async def stages_for_jobs(
    session: AsyncSession, job_ids: list[uuid.UUID]
) -> dict[uuid.UUID, StageSet]:
    rows = await session.execute(
        select(HirePipelineConfig.job_id, HirePipelineConfig.stages).where(
            HirePipelineConfig.job_id.in_(job_ids)
        )
    )
    found = {r.job_id: validate_stage_set(r.stages) for r in rows}
    return {job_id: found.get(job_id, DEFAULT_STAGES) for job_id in job_ids}


@dataclass(frozen=True, slots=True)
class SavedStages:
    stages: StageSet
    changed: bool


async def save_stages(session: AsyncSession, actor: Actor, job: HireJob, raw: Any) -> SavedStages:
    """Replace the job's stages. Audited (``hire.pipeline.stages_saved``)."""
    stages = validate_stage_set(raw)
    current = await stages_for(session, job.id)
    removed = set(current.keys()) - set(stages.keys())
    if removed:
        in_use = await session.scalar(
            select(func.count())
            .select_from(HirePipelineEntry)
            .where(HirePipelineEntry.job_id == job.id, HirePipelineEntry.stage_key.in_(removed))
        )
        if in_use:
            raise ConflictError("move the candidates out of a stage before you remove it")
    data = [s.model_dump(mode="json") for s in stages.stages]
    row = await session.scalar(
        select(HirePipelineConfig).where(HirePipelineConfig.job_id == job.id).with_for_update()
    )
    now = datetime.now(UTC)
    if row is None:
        row = HirePipelineConfig(
            id=uuid.uuid4(), org_id=job.org_id, job_id=job.id, stages=data, updated_by=actor.id
        )
        session.add(row)
    elif row.stages == data:
        return SavedStages(stages, changed=False)
    else:
        row.stages = data
        row.updated_by = actor.id
        row.updated_at = now
    await session.flush()
    await audit_actor_action(
        session,
        actor,
        "hire.pipeline.stages_saved",
        entity_type="hire.job",
        entity_id=job.id,
        workflow=WORKFLOW,
        payload={"stages": stages.keys(), "removed": sorted(removed)},
    )
    return SavedStages(stages, changed=True)
