# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Retention, dry run only (WP1.12).

* ``rules``        what may be deleted after how long (``RetentionRule``). Core declares
                   rules for technical rows only (expired idempotency keys, delivered
                   webhook events and deliveries, old inbound event ids, old retention
                   reports). Retention of business data (documents, candidates, invoices)
                   waits for the default periods to be decided (brief ASK item 6).
* ``models``       ``retention_reports``: per organisation, per run, per rule, how many
                   rows would be deleted. A check constraint keeps every row a dry run.
* ``service``      ``dry_run_org`` counts and records; nothing is ever deleted.
* ``permissions``  ``core.retention.read``.
"""
