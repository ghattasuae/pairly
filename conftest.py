# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Repository-wide pytest setup.

Encryption keys (WP1.14): the API, the worker and every encrypted column need
``PAIRLY_KEK_*`` and ``PAIRLY_BLIND_INDEX_KEY``; the API also needs
``PAIRLY_API_KEY_PEPPER`` (WP1.11). When the environment does not set
them, the test session gets fresh random keys (never a fixed value), so tests never
depend on a developer's real keys and no key is ever committed.
"""

from __future__ import annotations

import base64
import os
import secrets


def _random_key() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii")


if not os.environ.get("PAIRLY_KEK_CURRENT"):
    os.environ["PAIRLY_KEK_CURRENT"] = "test1"
    os.environ.setdefault("PAIRLY_KEK_test1", _random_key())
os.environ.setdefault("PAIRLY_BLIND_INDEX_KEY", _random_key())
# API key hashing pepper (WP1.11), random per session like the keys above.
os.environ.setdefault("PAIRLY_API_KEY_PEPPER", _random_key())
