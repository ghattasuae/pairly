# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""``uv run python -m evals.pay``: see ``evals/pay/__init__.py``."""

from __future__ import annotations

from evals.pay.run import main

if __name__ == "__main__":
    raise SystemExit(main())
