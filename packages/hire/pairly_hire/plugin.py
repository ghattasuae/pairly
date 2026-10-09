# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""The Pairly Hire plugin, registered in the ``pairly.workflows`` entry point group.

Declared on one object (``pairly_core.plugins.WorkflowPlugin`` plus optional attributes):

* ``permissions``  ``hire.*`` with default role grants (``permissions`` and
                   ``screening.permissions``).
* ``graphs``       ``hire_screening`` (``screening.spec``): CV to a person's shortlist
                   decision, with its ``hire.screening`` approval type and the
                   ``shortlist`` final action.
* ``api_router``   the ``/v1/hire`` routes, imported only when called.
* ``demo_seeds``   ``hire-demo`` for ``pairly-admin seed``.
* ``migrations_dir``  the ``hire`` Alembic branch (``migrations/``).
* ``prompt_roots``  the versioned prompts ``hire_jd_draft``, ``hire_rubric_draft`` and
                   ``hire_cv_score``.
* ``retention_rules``  ``hire.applications.rejected`` (dry run, per-organisation months).
* ``scheduled_jobs``  ``hire_screening_start``: starts the screening of submitted
                   applications whose CV is ready, every minute (``screening.start``).

Importing this module registers the Hire tables on ``Base.metadata``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from pairly_core.jobs.registry import JobSpec
from pairly_core.plugins import DemoSeed
from pairly_core.rbac import PermissionSpec
from pairly_core.retention.rules import RetentionRule
from pairly_core.workflow.spec import GraphSpec
from pairly_hire import models  # noqa: F401  (registers the Hire tables)
from pairly_hire.permissions import PERMISSIONS
from pairly_hire.retention import RETENTION_RULES
from pairly_hire.screening import models as screening_models  # noqa: F401  (its tables)
from pairly_hire.screening.permissions import SCREENING_PERMISSIONS
from pairly_hire.screening.spec import HIRE_SCREENING
from pairly_hire.screening.start import SCHEDULED_JOBS
from pairly_hire.seed import seed_hire_demo
from pairly_hire.selfid import models as selfid_models  # noqa: F401  (registers its table)

if TYPE_CHECKING:
    from fastapi import APIRouter

_HERE = Path(__file__).resolve().parent


def api_router() -> APIRouter:
    from pairly_hire.api import router

    return router


@dataclass(frozen=True, slots=True)
class HirePlugin:
    id: str = "hire"
    name: str = "Pairly Hire"
    permissions: tuple[PermissionSpec, ...] = field(default=(*PERMISSIONS, *SCREENING_PERMISSIONS))
    graphs: tuple[GraphSpec, ...] = (HIRE_SCREENING,)
    demo_seeds: Mapping[str, DemoSeed] = field(
        default_factory=lambda: {"hire-demo": seed_hire_demo}
    )
    migrations_dir: Path = _HERE / "migrations"
    prompt_roots: tuple[Path, ...] = (_HERE / "prompts",)
    retention_rules: tuple[RetentionRule, ...] = RETENTION_RULES
    scheduled_jobs: tuple[JobSpec, ...] = SCHEDULED_JOBS

    def api_router(self) -> APIRouter:
        return api_router()


plugin = HirePlugin()
