# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
# ruff: noqa: RUF001 (the confusables table maps lookalike letters on purpose)
"""Lookalike sender domains.

A domain is compared with every known vendor domain of the organisation. Before any
comparison it is IDN-decoded (``xn--`` labels to Unicode) and lower-cased. Then:

* ``homoglyph``: its *skeleton* equals a known domain's skeleton although the domains
  differ. The skeleton maps confusable characters to one Latin letter (Cyrillic and
  Greek lookalikes, ``0`` to ``o``, ``1``, ``i`` and ``l`` to ``l``, ``rn`` to ``m``,
  ``vv`` to ``w``, ...), after Unicode NFKC. ``idn`` when the domain was an IDN.
* ``tld_swap``: the same name under another ending (``vendor.example`` and
  ``vendor.test``).
* ``extra_text``: the known name plus a few letters or a hyphenated word
  (``vendor-ae``, ``vendorpay``).
* ``edit_distance``: one letter added, removed, changed or two swapped (names of at
  least five characters), or two edits for names of at least nine.

A subdomain of a known domain (``mail.vendor.example``) is the vendor's own.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Final

MAX_EXTRA: Final = 4
_CONFUSABLES: Final[dict[str, str]] = {
    # Cyrillic
    "а": "a", "в": "b", "е": "e", "ё": "e", "к": "k", "м": "m", "н": "h", "о": "o",
    "р": "p", "с": "c", "т": "t", "у": "y", "х": "x", "і": "l", "ї": "l", "ј": "j",
    "ѕ": "s", "ԁ": "d", "ɡ": "g", "ӏ": "l", "ү": "y", "һ": "h", "ԛ": "q", "ԝ": "w",
    # Greek
    "α": "a", "β": "b", "ε": "e", "ι": "l", "κ": "k", "ν": "v", "ο": "o", "ρ": "p",
    "τ": "t", "υ": "u", "χ": "x", "ω": "w",
    # Digits and Latin lookalikes
    "0": "o", "1": "l", "i": "l", "|": "l", "3": "e", "4": "a", "5": "s", "7": "t",
    "8": "b", "9": "g", "$": "s", "@": "a",
}  # fmt: skip
_SEQUENCES: Final = (("rn", "m"), ("vv", "w"), ("cl", "d"), ("nn", "m"))


@dataclass(frozen=True, slots=True)
class Lookalike:
    domain: str
    known: str
    how: str


def decode(domain: str) -> tuple[str, bool]:
    """The Unicode form of ``domain`` and whether any label was an IDN."""
    labels = domain.strip().strip(".").lower().split(".")
    out: list[str] = []
    idn = False
    for label in labels:
        if label.startswith("xn--"):
            try:
                out.append(label[4:].encode("ascii").decode("punycode"))
                idn = True
                continue
            except (UnicodeError, ValueError):
                pass
        out.append(label)
        if any(ord(ch) > 127 for ch in label):
            idn = True
    return ".".join(out), idn


def skeleton(text: str) -> str:
    folded = unicodedata.normalize("NFKC", text).casefold()
    mapped = "".join(_CONFUSABLES.get(ch, ch) for ch in folded)
    for seq, repl in _SEQUENCES:
        mapped = mapped.replace(seq, repl)
    return mapped.replace("-", "")


def _split(domain: str) -> tuple[str, str]:
    name, _, tld = domain.rpartition(".")
    return (name, tld) if name else (tld, "")


def _distance(a: str, b: str, limit: int) -> int:
    """Optimal string alignment distance (adjacent swaps count as one), capped."""
    if abs(len(a) - len(b)) > limit:
        return limit + 1
    previous2: list[int] = []
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        current = [i] + [0] * len(b)
        for j, cb in enumerate(b, start=1):
            cost = 0 if ca == cb else 1
            current[j] = min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + cost)
            if i > 1 and j > 1 and ca == b[j - 2] and a[i - 2] == cb:
                current[j] = min(current[j], previous2[j - 2] + 1)
        previous2, previous = previous, current
    return previous[-1]


def is_own(domain: str, known: str) -> bool:
    return domain == known or domain.endswith("." + known)


def lookalike(domain: str, known_domains: Iterable[str]) -> Lookalike | None:
    """The first known domain ``domain`` imitates, or None (also None when ``domain``
    is itself known or a subdomain of a known one)."""
    known_list = [k.strip().lower() for k in known_domains if k and k.strip()]
    raw = domain.strip().lower()
    if any(is_own(raw, k) for k in known_list):
        return None
    text, idn = decode(raw)
    name, tld = _split(text)
    name_skeleton = skeleton(name)
    for known in known_list:
        known_text, _ = decode(known)
        known_name, known_tld = _split(known_text)
        if not known_name:
            continue
        if text == known_text:
            continue
        if skeleton(text) == skeleton(known_text):
            return Lookalike(domain, known, "idn" if idn else "homoglyph")
        if name == known_name and tld != known_tld:
            return Lookalike(domain, known, "tld_swap")
        if name_skeleton == skeleton(known_name) and tld != known_tld:
            return Lookalike(domain, known, "idn" if idn else "homoglyph")
        base = name.rsplit(".", 1)[-1]
        known_base = known_name.rsplit(".", 1)[-1]
        rest = base.replace(known_base, "", 1)
        if (
            len(known_base) >= 4
            and base != known_base
            and (base.startswith(known_base) or base.endswith(known_base))
            and (len(rest) <= MAX_EXTRA or rest.startswith("-") or rest.endswith("-"))
        ):
            return Lookalike(domain, known, "extra_text")
        if len(known_base) >= 5:
            limit = 2 if len(known_base) >= 9 else 1
            if _distance(skeleton(base), skeleton(known_base), limit) <= limit:
                return Lookalike(domain, known, "idn" if idn else "edit_distance")
    return None
