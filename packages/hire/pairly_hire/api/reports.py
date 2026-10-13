# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Bias reporting routes (M3, brief 3.10): the bias and adverse-impact report and its
audit pack. The one place staff see anything derived from self-identification, and only
as counts per group with small groups suppressed (``pairly_hire.analytics.bias``).

* ``GET /bias?job_id=&since=&until=&min_sample=``  the report (``hire.bias.read``), each
  view audited (``hire.bias.report_viewed``)
* ``GET /bias/export?...``  the bias audit pack, a zip of ``data.csv``, ``results.csv``,
  ``report.json`` and ``METHOD.md`` (``hire.bias.export``: its ``export`` action needs a
  two-factor sign-in). Every export is audited (``hire.bias.exported``) with the scope,
  the settings and the SHA-256 of the file.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Query
from fastapi.responses import Response
from pydantic import BaseModel

from pairly_core.audit import audit_actor_action
from pairly_core.identity import CurrentActor, OrgSession
from pairly_core.rbac import authorize
from pairly_hire.analytics import bias
from pairly_hire.permissions import BIAS_EXPORT, BIAS_READ

router = APIRouter(tags=["hire"])

_MinSample = Annotated[int, Query(ge=5, le=1000)]


class HireBiasGroupOut(BaseModel):
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


class HireBiasStepOut(BaseModel):
    from_stage: str
    to_stage: str
    reference_group: str | None
    groups: list[HireBiasGroupOut]


class HireBiasCategoryOut(BaseModel):
    category: str
    label_en: str
    label_ar: str
    steps: list[HireBiasStepOut]


class HireBiasSettingsOut(BaseModel):
    min_sample: int
    min_group: int


class HireBiasReportOut(BaseModel):
    generated_at: str
    job_id: str | None
    since: str | None
    until: str | None
    applications: int
    settings: HireBiasSettingsOut
    stages: list[str]
    categories: list[HireBiasCategoryOut]
    fairness: dict[str, Any] | None
    notes: list[str]


async def _report(
    session: OrgSession,
    job_id: uuid.UUID | None,
    since: date | None,
    until: date | None,
    min_sample: int,
) -> bias.BiasReport:
    report = await bias.build_report(
        session,
        job_id=job_id,
        since=since,
        until=until,
        settings=bias.BiasSettings(min_sample=min_sample),
    )
    return report


@router.get("/bias", response_model=HireBiasReportOut)
async def bias_report(
    actor: CurrentActor,
    session: OrgSession,
    job_id: uuid.UUID | None = None,
    since: date | None = None,
    until: date | None = None,
    min_sample: _MinSample = bias.DEFAULT_MIN_SAMPLE,
) -> HireBiasReportOut:
    authorize(actor, BIAS_READ)
    report = await _report(session, job_id, since, until, min_sample)
    # Like every read of self-identification totals, each view is audited.
    await audit_actor_action(
        session,
        actor,
        "hire.bias.report_viewed",
        entity_type="hire.job" if job_id else None,
        entity_id=job_id,
        workflow="hire",
        payload={"applications": report.applications, "min_sample": min_sample},
    )
    return HireBiasReportOut.model_validate(report.as_dict())


@router.get("/bias/export", response_class=Response)
async def bias_export(
    actor: CurrentActor,
    session: OrgSession,
    job_id: uuid.UUID | None = None,
    since: date | None = None,
    until: date | None = None,
    min_sample: _MinSample = bias.DEFAULT_MIN_SAMPLE,
) -> Response:
    authorize(actor, BIAS_READ)
    authorize(actor, BIAS_EXPORT)
    report = await _report(session, job_id, since, until, min_sample)
    pack = bias.audit_pack(report)
    digest = bias.pack_digest(pack)
    await audit_actor_action(
        session,
        actor,
        "hire.bias.exported",
        entity_type="hire.bias_pack",
        entity_id=digest[:32],
        workflow="hire",
        payload={
            "sha256": digest,
            "job_id": report.job_id,
            "since": report.since,
            "until": report.until,
            "applications": report.applications,
            "min_sample": report.settings.min_sample,
            "min_group": report.settings.min_group,
        },
    )
    return Response(
        pack,
        media_type="application/zip",
        headers={
            "Cache-Control": "no-store",
            "Content-Disposition": 'attachment; filename="pairly-bias-audit-pack.zip"',
            "X-Content-Type-Options": "nosniff",
            "X-Pairly-Sha256": digest,
        },
    )
