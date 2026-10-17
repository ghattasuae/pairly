# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Hiring analytics (brief 3.10), computed from real tables only, never invented.

* ``facts``: the loader. It reads applications, stage moves, messages, screenings,
  overrides, model calls, interviews and approvals of the session's organisation (Row
  Level Security) into plain dataclasses. Self-identification is never read here.
* ``metrics``: pure functions over those facts. Every number carries the records it
  was computed from, so the page links each number to them.
* ``bias``: the adverse-impact report (four-fifths rule, small-sample warnings,
  suppression). It is the only module here that reaches self-identification, and only
  as counts per group, through ``pairly_hire.selfid.reporting``.
* ``stats``: exact statistics in the standard library (Fisher exact test, Cohen's
  kappa), no SciPy.
* ``fairness``: the counterfactual fairness runner (swapped names, gender markers and
  nationalities on copies of real screened applications).
* ``calibration``: recruiters' labels per criterion against the AI's scores.
"""
