# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Outbound and inbound webhooks (WP1.11).

* ``signature``  ``Pairly-Signature`` (``t=...,v1=...``) signing and verification.
* ``events``     the event type registry (core plus plugins) and ``publish_event``,
                 which writes the transactional outbox.
* ``models``     ``webhook_endpoints``, ``webhook_outbox``, ``webhook_deliveries``,
                 ``inbound_events``.
* ``ssrf``       endpoint URL checks: https, public addresses only, at creation and
                 again at every delivery (DNS rebinding).
* ``delivery``   the worker side: fan out, deliver, retry with backoff, auto-disable.
* ``service``    endpoint management.
* ``inbound``    deduplication of inbound provider events.
"""
