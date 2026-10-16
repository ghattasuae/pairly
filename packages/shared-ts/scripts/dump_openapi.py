# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Print the API's OpenAPI document without starting a server or touching a database.

Used by `pnpm gen:api`. Settings get inert placeholder values: building the app object
and its schema never connects anywhere (connections open in the lifespan, which does not
run here).
"""

from __future__ import annotations

import base64
import json
import os
import secrets
import sys

PLACEHOLDERS = {
    "PAIRLY_ENV": "test",
    "PAIRLY_DATABASE_URL": "postgresql://openapi:openapi@127.0.0.1:1/openapi",
    "PAIRLY_AUTH_JWKS_URL": "http://127.0.0.1:1/jwks",
    "PAIRLY_AUTH_ISSUER": "http://localhost:3000",
    "PAIRLY_AUTH_AUDIENCE": "http://localhost:3000",
    "PAIRLY_LOG_LEVEL": "WARNING",
}


def _random_key() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii")


# Startup refuses to run without keys (fail closed). The schema never uses them, so
# each run gets throwaway random ones (never a fixed value, never a real key).
RANDOM_KEYS = ("PAIRLY_KEK_openapi", "PAIRLY_BLIND_INDEX_KEY", "PAIRLY_API_KEY_PEPPER")


def build_schema() -> dict[str, object]:
    """The OpenAPI document of the API code (sets the placeholder settings first)."""
    for key, value in PLACEHOLDERS.items():
        os.environ[key] = value
    os.environ["PAIRLY_KEK_CURRENT"] = "openapi"
    for key in RANDOM_KEYS:
        os.environ[key] = _random_key()
    from pairly_api.main import create_app

    schema: dict[str, object] = create_app().openapi()
    return schema


def main() -> None:
    schema = build_schema()
    json.dump(schema, sys.stdout, indent=2, sort_keys=True, ensure_ascii=False)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
