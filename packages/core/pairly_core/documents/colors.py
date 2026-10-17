# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Colour normalisation to ``#rrggbb`` (gray, RGB and CMYK inputs)."""

from __future__ import annotations

from collections.abc import Sequence
from numbers import Real


def _channel(value: float) -> int:
    clamped = min(max(float(value), 0.0), 1.0)
    return round(clamped * 255)


def to_hex(value: object) -> str | None:
    """Normalise a PDF colour value (tuple of 1, 3 or 4 components in 0..1).

    Strings (pattern names) become ``pattern:<name>``; anything else is ``None``.
    """
    if value is None:
        return None
    if isinstance(value, str):
        return f"pattern:{value[:64]}"
    if isinstance(value, bytes):
        return f"pattern:{value[:64].decode('latin-1')}"
    if not isinstance(value, Sequence):
        if isinstance(value, Real):
            value = (value,)
        else:
            return None
    comps = list(value)
    if comps and isinstance(comps[-1], str | bytes):
        name = comps[-1]
        text = name.decode("latin-1") if isinstance(name, bytes) else name
        return f"pattern:{text[:64]}"
    if not all(isinstance(c, Real) for c in comps):
        return None
    floats = [float(c) for c in comps]
    if len(floats) == 1:
        r = g = b = _channel(floats[0])
    elif len(floats) == 3:
        r, g, b = (_channel(c) for c in floats)
    elif len(floats) == 4:
        c, m, y, k = (min(max(v, 0.0), 1.0) for v in floats)
        r = round(255 * (1 - c) * (1 - k))
        g = round(255 * (1 - m) * (1 - k))
        b = round(255 * (1 - y) * (1 - k))
    else:
        return None
    return f"#{r:02x}{g:02x}{b:02x}"
