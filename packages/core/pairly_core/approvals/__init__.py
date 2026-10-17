# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Human approvals: rules, chains, segregation of duties, delegation, SLA, executor.

Modules:

* ``models``: ``approvals``, ``approval_steps``, ``approval_decisions``,
  ``approval_delegations`` and ``approval_links``.
* ``rules``: the approval rules policy (YAML validated by Pydantic) and chain selection.
* ``service``: request approvals, record decisions, the inbox.
* ``executor``: runs a final action only after re-checking the approval server side.
* ``sla``: SLA states and the escalation job.
* ``hooks``: interfaces for decision records (WP1.10) and notifications (WP1.9).

This package imports nothing on purpose, so ``pairly_core.models`` can import the table
definitions without import cycles.
"""
