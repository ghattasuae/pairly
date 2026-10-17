# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Decision records, replay, receipts, appeals and compliance packs (WP1.10, brief 2.3).

Modules:

* ``models``: ``decision_records``, ``decision_replays``, ``appeals``,
  ``compliance_exports``.
* ``ids``: stable ``dec_...`` ids.
* ``service``: ``record_decision`` (the one way to write a record) and reads.
* ``hooks``: the approval hook and the calls the extraction, risk and policy services
  make after they store a result.
* ``replay``: re-run a decision from its record and diff the result.
* ``receipts``: role-redacted receipt (JSON) and PDF (English and Arabic).
* ``appeals``: create, resolve and the SLA job.
* ``compliance``: the compliance pack export (zip with a SHA-256 manifest).
* ``permissions``: ``core.decision.*``, ``core.appeal.*`` and ``core.compliance.export``.

This package imports nothing on purpose, so ``pairly_core.models`` can import the table
definitions without import cycles.
"""
