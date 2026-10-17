# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Notifications: templates, preferences, email, Slack and in-app delivery (WP1.9).

Modules:

* ``templating``    the sandboxed template language, variable types, lint and rendering.
* ``defaults``      system default templates (seeded by migrations, ``org_id`` NULL).
* ``templates``     template versions: create (lint), resolve, preview.
* ``preferences``   per-user channel, digest, snooze and quiet hours rules.
* ``service``       ``notify``: render, apply preferences, gate sensitive templates, queue.
* ``email``         ``EmailTransport`` (SMTP, Resend, fake) and the RFC 5322 message.
* ``delivery``      the worker side: send with retries and backoff, build daily digests.
* ``approval_hook`` approval emails, Slack messages and in-app items with signed links.
* ``receipts``      receipt replies to inbound email, with the backscatter rules.
* ``settings``      environment settings (``PAIRLY_EMAIL_*``, ``PAIRLY_SLACK_*``).

This package imports nothing on purpose, so ``pairly_core.models`` can import the table
definitions without import cycles.
"""
