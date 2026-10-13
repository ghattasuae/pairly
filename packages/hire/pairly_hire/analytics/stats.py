# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Exact statistics with the standard library only (no SciPy).

* ``fisher_exact``: the two-sided p-value of Fisher's exact test on a 2x2 table, from
  the hypergeometric distribution (``math.comb``), summing every table with the same
  margins that is no more likely than the observed one (the usual definition, as in R's
  ``fisher.test`` and SciPy's ``fisher_exact``).
* ``cohen_kappa``: agreement between two raters beyond chance, unweighted, over any
  categorical labels; ``None`` when it is undefined (one category only).
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Hashable, Sequence
from typing import Final

# Tolerance when comparing table probabilities (floating point ties).
_REL: Final = 1e-7


def _hypergeom(a: int, row1: int, col1: int, n: int) -> float:
    return math.comb(row1, a) * math.comb(n - row1, col1 - a) / math.comb(n, col1)


def fisher_exact(a: int, b: int, c: int, d: int) -> float:
    """Two-sided p-value for the table ``[[a, b], [c, d]]`` (non-negative counts)."""
    if min(a, b, c, d) < 0:
        raise ValueError("counts must be non-negative")
    n = a + b + c + d
    if n == 0:
        return 1.0
    row1, col1 = a + b, a + c
    low, high = max(0, col1 - (n - row1)), min(row1, col1)
    observed = _hypergeom(a, row1, col1, n)
    p = sum(
        prob
        for x in range(low, high + 1)
        if (prob := _hypergeom(x, row1, col1, n)) <= observed * (1 + _REL)
    )
    return min(1.0, p)


def cohen_kappa(first: Sequence[Hashable], second: Sequence[Hashable]) -> float | None:
    """Unweighted Cohen's kappa of two equally long label sequences."""
    if len(first) != len(second):
        raise ValueError("the two raters must label the same items")
    n = len(first)
    if n == 0:
        return None
    observed = sum(1 for x, y in zip(first, second, strict=True) if x == y) / n
    left, right = Counter(first), Counter(second)
    expected = sum(left[k] * right.get(k, 0) for k in left) / (n * n)
    if math.isclose(expected, 1.0):
        return None
    return (observed - expected) / (1 - expected)
