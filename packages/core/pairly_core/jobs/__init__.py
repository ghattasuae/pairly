# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Scheduled jobs base (WP1.12).

* ``registry``  every scheduled job is declared (``JobSpec``: name, description,
                schedule); core declares ``CORE_JOBS``, a workflow plugin may add its own
                through an optional ``scheduled_jobs`` attribute, under its namespace.
                The worker refuses to schedule a job that is not declared.
* ``runs``      ``job_runs`` history: ``tracked`` wraps a job so every run records its
                start, finish, status and a small summary of counts.
"""
