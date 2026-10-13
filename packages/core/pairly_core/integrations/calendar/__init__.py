# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Calendar and meeting providers (brief 3.6, ADR 0011).

* ``base``       the interface: free/busy, events with an ICS UID, meeting links.
* ``fake``       deterministic calendars for development, tests and the demo.
* ``google``     Google Calendar and Meet, OAuth per person (off until configured).
* ``microsoft``  Microsoft 365 (Graph) and Teams, OAuth per person (off until configured).
* ``zoom``       Zoom meeting links, Server-to-Server OAuth per organisation (off until
                 configured), combined with a calendar provider.
* ``http``       status codes and JSON bodies shared by ``microsoft`` and ``zoom``.

This package imports nothing on purpose (see ``pairly_core.notifications``).
"""
