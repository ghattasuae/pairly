# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Robust statistics for amount anomalies (median and MAD, exact Decimal arithmetic).

The modified z-score (Iglewicz and Hoaglin) is ``0.6745 * (x - median) / MAD``. It is
not dragged by the outliers it is looking for, unlike a mean and standard deviation.
When MAD is 0 (more than half the history is one value), the mean absolute deviation
scaled by 1.2533 is used instead; when that is 0 too (all history identical), any
different amount is an outlier with an infinite score.

Pay reuses ``amount_outlier`` for "unusual amount for this vendor".
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

MAD_SCALE = Decimal("0.6745")
MEANAD_SCALE = Decimal("1.2533")
DEFAULT_THRESHOLD = Decimal("3.5")
DEFAULT_MIN_HISTORY = 5


def median(values: Sequence[Decimal]) -> Decimal:
    if not values:
        raise ValueError("median of nothing")
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


def mad(values: Sequence[Decimal], center: Decimal | None = None) -> Decimal:
    """Median absolute deviation (unscaled)."""
    c = median(values) if center is None else center
    return median([abs(v - c) for v in values])


@dataclass(frozen=True, slots=True)
class OutlierResult:
    enough_history: bool
    is_outlier: bool
    # None when there is not enough history; Decimal("Infinity") when history is constant.
    z_score: Decimal | None
    median: Decimal | None
    mad: Decimal | None
    history_size: int


def amount_outlier(
    value: Decimal,
    history: Sequence[Decimal],
    *,
    threshold: Decimal = DEFAULT_THRESHOLD,
    min_history: int = DEFAULT_MIN_HISTORY,
) -> OutlierResult:
    """Is ``value`` an outlier against ``history``? Pure, exact, deterministic."""
    if threshold <= 0:
        raise ValueError("threshold must be positive")
    clean = [Decimal(v) for v in history if Decimal(v).is_finite()]
    if len(clean) < max(min_history, 1):
        return OutlierResult(False, False, None, None, None, len(clean))
    center = median(clean)
    spread = mad(clean, center)
    deviation = value - center
    if spread > 0:
        z = MAD_SCALE * deviation / spread
    else:
        mean_ad = sum((abs(v - center) for v in clean), Decimal(0)) / len(clean)
        if mean_ad > 0:
            z = deviation / (MEANAD_SCALE * mean_ad)
        else:
            z = Decimal(0) if deviation == 0 else Decimal("Infinity").copy_sign(deviation)
    return OutlierResult(
        enough_history=True,
        is_outlier=abs(z) > threshold,
        z_score=z,
        median=center,
        mad=spread,
        history_size=len(clean),
    )
