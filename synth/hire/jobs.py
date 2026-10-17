# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""The three sample jobs and their rubrics (``synth/hire/jobs/*.yaml``)."""

from __future__ import annotations

from functools import cache
from pathlib import Path
from typing import Final

import yaml

from synth.hire.models import Job

JOBS_DIR: Final = Path(__file__).resolve().parent / "jobs"
JOB_IDS: Final = ("backend_engineer", "ap_accountant", "recruiter")


@cache
def load_jobs() -> tuple[Job, ...]:
    jobs = []
    for job_id in JOB_IDS:
        data = yaml.safe_load((JOBS_DIR / f"{job_id}.yaml").read_text(encoding="utf-8"))
        job = Job.model_validate(data)
        if job.id != job_id:
            raise ValueError(f"{job_id}.yaml declares id {job.id!r}")
        jobs.append(job)
    return tuple(jobs)
