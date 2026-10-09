# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Hire routes, mounted by the API under ``/v1/hire`` (``HirePlugin.api_router``).

* ``jobs``: ``/v1/hire/jobs...`` jobs, AI drafts, text checks, rubric builder, forms.
* ``applications``: ``/v1/hire/applications...``, the self-identification report and
  the Hire settings.
* ``public``: ``/v1/hire/public/careers/...`` the careers pages' data, job feeds and the
  apply flow. No sign-in; rate limited; open jobs of visible organisations only.

Imported only by the API process (the worker never loads routes).
"""

from __future__ import annotations

from fastapi import APIRouter

from pairly_hire.api import applications, jobs, public

router = APIRouter()
router.include_router(jobs.router)
router.include_router(applications.router)
router.include_router(public.router)
