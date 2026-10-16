# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Interviews and feedback (brief 3.7, M3).

* ``plan``: the interview plan of a job: stages, interviewers per stage, and a scorecard
  per stage mapped to rubric criteria (3 to 5 questions, a 1 to 4 rating per criterion
  with anchor descriptions).
* ``kits``: interview kits per candidate and stage (model router, recorded for the demo).
* ``scorecards``: independent scorecards, hidden from the other panelists until you
  submit yours; ``links``: the interviewer's one-click scorecard link and session;
  ``reminders``: feedback reminders 2 and 24 hours after the interview.
* ``debrief``: the debrief summary for the hiring manager; humans decide.
"""
