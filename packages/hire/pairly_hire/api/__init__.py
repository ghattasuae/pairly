# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Hire routes, mounted by the API under ``/v1/hire`` (``HirePlugin.api_router``).

* ``jobs``: ``/v1/hire/jobs...`` jobs, AI drafts, text checks, rubric builder, forms.
* ``applications``: ``/v1/hire/applications...``, the self-identification report and
  the Hire settings.
* ``public``: ``/v1/hire/public/careers/...`` the careers pages' data, job feeds and the
  apply flow. No sign-in; rate limited; open jobs of visible organisations only.
* ``screening``: ``/v1/hire/applications/{id}/screening...`` the explainability panel,
  score overrides and re-runs, and ``/v1/hire/screening/settings``.
* ``screening_mode``: ``/v1/hire/jobs/{id}/screening-queue``, the recruiter's screening
  queue (WP2.6).
* ``pipeline``: boards, stages, moves and undo, bulk moves, profiles, notes, tags, the
  waiting list, talent rediscovery and referrals (WP2.5).
* ``messaging``: message templates, drafts, AI drafts, send and submit for approval,
  bulk updates, the SLA setting and the candidates' own preferences (WP2.7).
* ``portal``: ``/v1/hire/portal/...`` the candidate portal (WP2.9): magic links, the
  candidate's own applications, messages, files, withdrawal and privacy centre. Its own
  candidate session, never a staff credential.
* ``candidate_requests``: the hiring team's file requests to candidates and the admins'
  queue of candidates' data requests (WP2.9).
* ``scheduling``: interviews, free times, self-scheduling links, availability, calendar
  integrations, and the candidates' scheduling page (WP2.8).
* ``requisitions``: role requests, their approval and opening a job from one (M3).
* ``interviewing``: interview plans, kits, scorecards and debriefs (M3).

Imported only by the API process (the worker never loads routes).
"""

from __future__ import annotations

from fastapi import APIRouter

from pairly_hire.api import (
    applications,
    candidate_requests,
    interviewing,
    jobs,
    messaging,
    pipeline,
    portal,
    public,
    requisitions,
    scheduling,
    screening,
    screening_mode,
)

router = APIRouter()
router.include_router(jobs.router)
router.include_router(applications.router)
router.include_router(public.router)
router.include_router(screening.router)
router.include_router(screening_mode.router)
router.include_router(pipeline.router)
router.include_router(messaging.router)
router.include_router(portal.router)
router.include_router(candidate_requests.router)
router.include_router(scheduling.router)
router.include_router(requisitions.router)
router.include_router(interviewing.router)
