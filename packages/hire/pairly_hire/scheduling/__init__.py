# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Interview scheduling (brief 3.6, WP2.8).

* ``models``       the tables (migration ``hire_0005``).
* ``rules``        the organisation's settings and each interviewer's availability.
* ``providers``    which calendar each person uses (fake by default, Google when the
                   flag, the OAuth client and the person's connection are all there).
* ``service``      find times, book, links, move, cancel, outcomes (no double booking).
* ``emails``       candidate and interviewer emails with calendar files (blind-safe).
* ``reminders``    the ``hire_interview_reminders`` job (24 hours and 1 hour before).
* ``tokens``       the candidate's signed single-use-per-booking link.
* ``api``          the interface for other Hire features (screening mode, portal).

Engine parts: ``pairly_core.scheduling`` (availability math, ICS files) and
``pairly_core.integrations.calendar`` (providers). ADR 0011.
"""
