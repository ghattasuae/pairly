# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""``uv run python -m evals.live``: see ``evals/live/__init__.py``."""

from __future__ import annotations

from evals.live.run import main

if __name__ == "__main__":
    raise SystemExit(main())
