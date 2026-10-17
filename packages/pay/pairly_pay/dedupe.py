# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Vendor name matching for dedupe (brief 4.1.1). Pure functions, no I/O.

``name_key`` reduces a company name to what identifies it: Unicode NFKC, case folded,
punctuation removed, Arabic letter variants unified, and legal form words dropped
("LLC", "FZE", "Ltd", "GmbH", "ذ.م.م" ...), so "Seltus IT Solutions L.L.C." and
"SELTUS IT SOLUTIONS LLC" have the same key.

``name_similarity`` compares two keys: the mean of a character ratio over the sorted
words and a character ratio over the words only one name has (so shared generic words
such as "IT Solutions" do not make two companies alike). 1.0 is the same name. ``SIMILAR`` is the
threshold above which two vendors are shown as possible duplicates; a person decides.
"""

from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher
from typing import Final

SIMILAR: Final = 0.86
MAX_NAME: Final = 300

# Legal forms and filler words that do not tell two companies apart.
_LEGAL: Final = frozenset(
    {
        "llc",
        "l l c",
        "fze",
        "fzco",
        "fzc",
        "fz",
        "ltd",
        "limited",
        "plc",
        "pjsc",
        "psc",
        "pvt",
        "inc",
        "corp",
        "corporation",
        "co",
        "company",
        "est",
        "establishment",
        "gmbh",
        "ag",
        "sarl",
        "sa",
        "sas",
        "bv",
        "nv",
        "wll",
        "spc",
        "the",
        "and",
        "of",
        "ذمم",
        "شذمم",
        "مؤسسة",
        "شركة",
        "ش",
        "م",
        "ذ",
    }
)
_PUNCT: Final = re.compile(r"[^\w\s]", re.UNICODE)
_SPACE: Final = re.compile(r"\s+")
_ARABIC: Final = str.maketrans(
    {
        "\u0623": "\u0627",
        "\u0625": "\u0627",
        "\u0622": "\u0627",
        "\u0629": "\u0647",
        "\u0649": "\u064a",
        "\u0640": "",
    }
)


def name_key(name: str) -> str:
    text = unicodedata.normalize("NFKC", (name or "")[:MAX_NAME]).casefold()
    text = text.translate(_ARABIC)
    # Dotted forms first ("L.L.C.", "W.L.L", "ذ.م.م"), then the rest of the punctuation.
    text = re.sub(r"\b(\w)\.(\w)\.(\w)\.?", r"\1\2\3", text)
    text = re.sub(r"\b(\w)\.(\w)\.?(?=\s|$)", r"\1\2", text)
    text = _PUNCT.sub(" ", text).replace("_", " ")
    tokens = [t for t in _SPACE.split(text) if t and t not in _LEGAL]
    return " ".join(tokens)


def name_similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    ta, tb = a.split(), b.split()
    sa, sb = set(ta), set(tb)
    if sa == sb:
        return 1.0
    whole = SequenceMatcher(None, " ".join(sorted(ta)), " ".join(sorted(tb))).ratio()
    # What tells the names apart: the words only one of them has. Shared generic words
    # ("IT Solutions") must not make two different companies look alike.
    only_a, only_b = " ".join(sorted(sa - sb)), " ".join(sorted(sb - sa))
    distinct = SequenceMatcher(None, only_a, only_b).ratio() if only_a and only_b else 0.0
    return round((whole + distinct) / 2, 4)
