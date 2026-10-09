# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Screening (brief 3.3): from a submitted application to a person's shortlist decision.

* ``graph``      the ``hire_screening`` graph (version 1), one run per application.
* ``pipeline``   the steps as plain functions (graph, recordings and evals share them).
* ``profile``    CV parsing into a structured profile with evidence (``CvProfile``).
* ``blind``      blind mode: what the scoring model and reviewers do not see.
* ``claims``     claim consistency checks and AI-generated signals (information only).
* ``knockouts``  knockout answers to a person with a suggested action, never a rejection.
* ``service``    rows, settings, decision records and replay handlers.
* ``actions``    the ``shortlist`` final action (lifts blind mode).
* ``models``     ``hire_screenings`` and ``hire_screening_settings`` (migration hire_0002).
* ``permissions``  ``hire.screening.*``.

Scoring itself (prompt contract, no evidence no credit, the overall score) lives in
``pairly_hire.scoring``. Neither package may import the voluntary self-identification
answers (``pairly_hire.selfid``), the form builder or the apply flow: the import-linter
contract in the root ``pyproject.toml`` and ``tests/test_selfid_isolation.py`` check it.
"""
