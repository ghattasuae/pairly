# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Retention with automatic deletion (WP1.12, brief 6.9).

* ``rules``        what may be deleted after how long and how (``RetentionRule``): core
                   declares technical rows (expired idempotency keys, delivered webhook
                   events and deliveries, old inbound event ids, old retention reports);
                   plugins add their own, such as Hire's rejected and withdrawn
                   candidates, erased one by one through Hire's candidate erasure.
* ``models``       reports, per-organisation settings (on or off, period, daily cap), the
                   seven-day preview list and legal holds.
* ``service``      the daily run: plan (list and report), then delete what the settings
                   say, audited, each deletion a decision record; settings and holds.
* ``permissions``  ``core.retention.read`` and ``core.retention.manage``.
"""
