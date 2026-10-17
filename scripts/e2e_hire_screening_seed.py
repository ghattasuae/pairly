# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Development seed for the screening mode end-to-end test. Never runs in production.

    PAIRLY_DEVSEED=1 python scripts/e2e_hire_screening_seed.py --org-slug acme \
        --count 20 --out /tmp/cvs

In the organisation (it must exist and have the Hire seed, ``make demo-hire``):

1. the synthetic "Backend Engineer" job (``synth.hire.jobs``, slug
   ``backend-engineer-screening``), open, with its rubric locked as the seed (the
   label guide anchors, as ``synth.hire.screening.screening_rubric``) and the default
   application form;
2. the recorded screening responses of the synthetic demo applications
   (``synth/hire/recordings/hire_screening.json``: scripted from synthetic ground
   truth, not model output), so screening runs offline in ``recorded`` mode.

It writes ``--count`` synthetic CVs (the demo applications first, then clean PDF CVs of
the synthetic corpus) to ``--out`` with ``manifest.json``: the file name, the contact
details and the knockout answers the e2e applies with through the public careers API.
CVs without a recording are screened too and reach a person without AI scores (fail
closed), as they would without a model.

Connects as the owner role (``PAIRLY_DATABASE_URL_OWNER``), like ``pairly-admin``.
Refuses to run unless ``PAIRLY_DEVSEED=1`` and ``PAIRLY_ENV`` (when set) is
``development`` or ``test``. Prints one JSON line with the job id and slug.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import create_async_engine

import pairly_core.models  # noqa: F401  (registers every core table)
from pairly_core.audit import ActorType, append_audit
from pairly_core.config import to_asyncpg_url
from pairly_core.crypto.keys import init_crypto
from pairly_core.db import create_sessionmaker, set_tenant
from pairly_core.llm.bundle import import_recordings, parse_bundle
from pairly_core.policy_versions import publish_policy_version
from pairly_hire import models  # noqa: F401  (registers the Hire tables)
from pairly_hire.forms import DEFAULT_FORM, form_validator
from pairly_hire.forms import POLICY_KIND as FORM_KIND
from pairly_hire.models import HireJob, JobStatus
from pairly_hire.rubric import POLICY_KIND as RUBRIC_KIND
from pairly_hire.rubric import policy_name, rubric_validator

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    # The synthetic data package lives at the repository root (run as a plain script).
    sys.path.insert(0, str(ROOT))

from synth.hire.dataset import build  # noqa: E402
from synth.hire.screening import (  # noqa: E402
    DEMO_APPLICATIONS,
    DEMO_JOB,
    contact_for,
    job_by_id,
    knockout_answers,
    recordings_text,
    screening_rubric,
    upload_name,
)

OWNER_URL_ENV = "PAIRLY_DATABASE_URL_OWNER"
ALLOWED_ENVS = frozenset({"development", "test"})
SEED_ACTOR = "pairly.devseed.hire-screening"
JOB_SLUG = "backend-engineer-screening"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


async def _org_id(owner_url: str, slug: str) -> uuid.UUID | None:
    engine = create_async_engine(to_asyncpg_url(owner_url), pool_size=1, max_overflow=0)
    try:
        async with engine.connect() as conn:
            found = await conn.scalar(
                text("SELECT admin_org_id_by_slug(:slug)"), {"slug": slug.strip().lower()}
            )
            return found if isinstance(found, uuid.UUID) else None
    finally:
        await engine.dispose()


async def seed(owner_url: str, org_slug: str) -> dict[str, str]:
    org_id = await _org_id(owner_url, org_slug)
    if org_id is None:
        raise SystemExit(f"no organisation {org_slug!r}")
    init_crypto()
    engine = create_async_engine(to_asyncpg_url(owner_url), pool_size=1, max_overflow=0)
    sessionmaker = create_sessionmaker(engine)
    job = job_by_id(DEMO_JOB)
    rubric = screening_rubric(job)
    try:
        session = sessionmaker()
        set_tenant(session, org_id, None)
        async with session, session.begin():
            row = await session.scalar(select(HireJob).where(HireJob.slug == JOB_SLUG))
            if row is None:
                now = datetime.now(UTC)
                row = HireJob(
                    id=uuid.uuid4(),
                    org_id=org_id,
                    slug=JOB_SLUG,
                    title=job.title,
                    title_ar="",
                    location="Dubai, United Arab Emirates",
                    country="AE",
                    work_model="hybrid",
                    employment_type="full_time",
                    description_en=job.summary,
                    status=JobStatus.DRAFT.value,
                    created_by=None,
                )
                session.add(row)
                await session.flush()
                name = policy_name(row.id.hex)
                locked = await publish_policy_version(
                    session,
                    kind=RUBRIC_KIND,
                    name=name,
                    content=rubric.to_yaml(),
                    validate=rubric_validator(),
                    actor_type=ActorType.SYSTEM,
                    actor_id=SEED_ACTOR,
                )
                form = await publish_policy_version(
                    session,
                    kind=FORM_KIND,
                    name=name,
                    content=DEFAULT_FORM.to_yaml(),
                    validate=form_validator(),
                    actor_type=ActorType.SYSTEM,
                    actor_id=SEED_ACTOR,
                )
                row.rubric_policy_id = locked.id
                row.form_policy_id = form.id
                row.status = JobStatus.OPEN.value
                row.published_at = now
                await session.flush()
                await append_audit(
                    session,
                    action="hire.job.published",
                    actor_type=ActorType.SYSTEM,
                    actor_id=SEED_ACTOR,
                    entity_type="hire.job",
                    entity_id=row.id,
                    workflow="hire",
                    payload={"slug": JOB_SLUG, "seed": True},
                )
            await import_recordings(session, org_id, parse_bundle(recordings_text()))
            return {"job_id": str(row.id), "job_slug": JOB_SLUG, "org_id": str(org_id)}
    finally:
        await engine.dispose()


def write_cvs(out: Path, count: int) -> list[dict[str, object]]:
    specs = build(300, 2026)
    by_id = {s.record.id: s for s in specs}
    chosen = [by_id[i] for i in DEMO_APPLICATIONS if i in by_id]
    for spec in specs:
        if len(chosen) >= count:
            break
        record = spec.record
        clean = record.id.startswith("cv-") and record.format == "pdf" and record.attack is None
        labelled = any(lab.job_id == DEMO_JOB for lab in record.labels)
        if clean and labelled and spec not in chosen:
            chosen.append(spec)
    out.mkdir(parents=True, exist_ok=True)
    manifest: list[dict[str, object]] = []
    for index, spec in enumerate(chosen[:count]):
        name = f"{index:02d}-{upload_name(spec)}"
        (out / name).write_bytes(spec.render())
        contact = contact_for(spec)
        manifest.append(
            {
                "id": spec.record.id,
                "file": name,
                # The name to upload it under (the recordings were made with it).
                "upload_name": upload_name(spec),
                "media_type": DOCX if name.endswith(".docx") else "application/pdf",
                "contact": contact,
                "knockouts": knockout_answers(spec, DEMO_JOB),
            }
        )
    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    return manifest


def main(argv: list[str] | None = None) -> int:
    env = os.environ.get("PAIRLY_ENV", "development")
    if os.environ.get("PAIRLY_DEVSEED") != "1" or env not in ALLOWED_ENVS:
        print("this seed only runs with PAIRLY_DEVSEED=1 outside production", file=sys.stderr)
        return 2
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--org-slug", required=True)
    parser.add_argument("--count", type=int, default=20)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    owner_url = os.environ.get(OWNER_URL_ENV)
    if not owner_url:
        print(f"{OWNER_URL_ENV} is not set", file=sys.stderr)
        return 2
    result = asyncio.run(seed(owner_url, args.org_slug))
    manifest = write_cvs(args.out, max(1, min(args.count, 60)))
    print(json.dumps({**result, "cvs": len(manifest), "out": str(args.out)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
