# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Scoped API keys for the public API (WP1.11).

* ``tokens``      key format, the server pepper and the secret hash.
* ``models``      the ``api_keys`` table (org scoped, RLS forced).
* ``service``     create, list, revoke, and authenticate a request (``api_key`` actor).
* ``ratelimit``   per-key request rate limit.
* ``permissions`` ``core.api_keys.manage``.

A key is ``pk_live_<8 chars>_<secret>`` (or ``pk_test_`` for sandbox organisations).
The first part (``pk_live_<8 chars>``) is the visible prefix and identifies the key;
the secret is shown once at creation and only its keyed hash is stored.
"""
