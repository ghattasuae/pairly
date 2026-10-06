# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Print the API's OpenAPI document without starting a server or touching a database.

Used by `pnpm gen:api`. Settings get inert placeholder values: building the app object
and its schema never connects anywhere (connections open in the lifespan, which does not
run here).
"""

from __future__ import annotations

import json
import os
import sys

PLACEHOLDERS = {
    "PAIRLY_ENV": "test",
    "PAIRLY_DATABASE_URL": "postgresql://openapi:openapi@127.0.0.1:1/openapi",
    "PAIRLY_AUTH_JWKS_URL": "http://127.0.0.1:1/jwks",
    "PAIRLY_AUTH_ISSUER": "http://localhost:3000",
    "PAIRLY_AUTH_AUDIENCE": "http://localhost:3000",
    "PAIRLY_LOG_LEVEL": "WARNING",
}


def main() -> None:
    for key, value in PLACEHOLDERS.items():
        os.environ[key] = value
    from pairly_api.main import create_app

    schema = create_app().openapi()
    json.dump(schema, sys.stdout, indent=2, sort_keys=True, ensure_ascii=False)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
