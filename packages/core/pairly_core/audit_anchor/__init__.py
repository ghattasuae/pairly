# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Audit log hardening (WP1.10b).

Modules:

* ``models``: ``audit_verifications`` and ``audit_denial_limits``.
* ``signing``: Ed25519 signing of chain heads (``PAIRLY_AUDIT_SIGNING_KEY``).
* ``store``: write-once storage of signed heads outside the database.
* ``anchoring``: the nightly anchoring job and loading verified anchors.
* ``denials``: the per-actor token bucket for authorisation denial events.
* ``verification``: chain verification as a worker job with a result row.

This package imports nothing on purpose, so ``pairly_core.models`` can import the table
definitions without import cycles.
"""
