# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Scheduling (brief 2.2 item 12): workflow-agnostic time math and calendar files.

* ``availability``  working hours, buffers, daily caps and holidays per person; the
                    times when every person on a panel is free (time zone and DST safe).
* ``ics``           RFC 5545 calendar files (``METHOD:REQUEST`` and ``CANCEL``), written
                    by hand, plus a small reader for checks and tests.

Calendar providers (free/busy, events, meeting links) live in
``pairly_core.integrations.calendar``. Workflows keep their own tables (who is on a
panel, which interviews exist) and call these pure functions. ADR 0011.

This package imports nothing on purpose (see ``pairly_core.notifications``).
"""
