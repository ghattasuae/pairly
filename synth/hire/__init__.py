# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Synthetic Hire data (WP2.1): jobs with rubrics, CVs, attack CVs, fairness pairs.

* ``models``      Pydantic models: rubric YAML shape, CV ground truth, manifest records.
* ``jobs``        the three sample jobs (``jobs/*.yaml``).
* ``vocab``       role families, titles, skills, achievement templates (en, ar).
* ``facts``       a person and a career, from seeded random and Faker.
* ``text``        the CV's lines in English, Arabic or both, and its ground truth.
* ``labels``      deterministic rubric labels (synthetic, not human judgments).
* ``render_*``    PDF (reportlab), DOCX (python-docx), scans (Pillow).
* ``attacks``     attack CVs with what a detector should flag.
* ``fairness``    counterfactual pairs.
* ``dataset``     builds everything as ``DocSpec`` (record plus lazy render).
* ``recordings``  scripted classifier responses for offline tests.

Run ``uv run python -m synth.hire --help``. See ``synth/hire/README.md``.
"""
