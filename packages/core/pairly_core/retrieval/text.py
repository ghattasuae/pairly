# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Text normalisation shared by indexing, querying and quote checks (English and Arabic)."""

from __future__ import annotations

import re
import unicodedata
from typing import Final

_WORD_RE = re.compile(r"\w+", re.UNICODE)
_SPACE_RE = re.compile(r"\s+")
# Arabic diacritics (harakat), superscript alef and tatweel.
_ARABIC_MARKS_RE = re.compile(r"[\u064b-\u065f\u0670\u0640]")
_ALEF_RE = re.compile(r"[\u0622\u0623\u0625\u0671]")

# Small English list: words that carry no meaning for policy lookup. Arabic function
# words are short and handled by the length filter plus the "ال" prefix strip.
STOPWORDS: Final = frozenset(
    [
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "been",
        "but",
        "by",
        "can",
        "could",
        "did",
        "do",
        "does",
        "for",
        "from",
        "had",
        "has",
        "have",
        "he",
        "her",
        "his",
        "i",
        "if",
        "in",
        "into",
        "is",
        # "it" is kept on purpose: "IT approval" names a department.
        "its",
        "may",
        "might",
        "must",
        "not",
        "of",
        "on",
        "or",
        "our",
        "shall",
        "she",
        "should",
        "so",
        "such",
        "than",
        "that",
        "the",
        "their",
        "them",
        "then",
        "there",
        "these",
        "they",
        "this",
        "those",
        "to",
        "too",
        "us",
        "was",
        "we",
        "were",
        "what",
        "when",
        "where",
        "which",
        "who",
        "whom",
        "why",
        "will",
        "with",
        "would",
        "you",
        "your",
    ]
)

# Typographic variants a model may use when quoting; folded before quote comparison.
_FOLD = str.maketrans(
    {
        "\u2018": "'",
        "\u2019": "'",
        "\u201a": "'",
        "\u201b": "'",
        "\u201c": '"',
        "\u201d": '"',
        "\u201e": '"',
        "\u00ab": '"',
        "\u00bb": '"',
        "\u2013": "-",
        "\u2014": "-",
        "\u2212": "-",
        "\u00a0": " ",
        "\u066c": ",",
        "\u060c": ",",
    }
)


def normalize_arabic(text: str) -> str:
    text = _ARABIC_MARKS_RE.sub("", text)
    text = _ALEF_RE.sub("\u0627", text)
    return text.replace("\u0649", "\u064a").replace("\u0629", "\u0647")


def fold(text: str) -> str:
    """Canonical form for exact-substring checks: NFKC, case folded, quotes and dashes
    unified, Arabic marks removed, whitespace collapsed."""
    text = unicodedata.normalize("NFKC", text).translate(_FOLD)
    text = normalize_arabic(text).casefold()
    return _SPACE_RE.sub(" ", text).strip()


def _stem(word: str) -> str:
    if word.startswith("\u0627\u0644") and len(word) > 4:  # Arabic definite article
        return word[2:]
    if not word.isascii():
        return word
    for suffix in ("ies", "ing", "ed", "s"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            word = word[: -len(suffix)] + ("y" if suffix == "ies" else "")
            break
    # "purchase", "purchases" and "purchased" all end up as "purchas".
    return word[:-1] if word.endswith("e") and len(word) > 4 else word


def terms(text: str, *, keep_stopwords: bool = False) -> list[str]:
    """Normalised word terms, in order: folded, light stemming, no stopwords."""
    out: list[str] = []
    for word in _WORD_RE.findall(fold(text)):
        word = word.strip("_")
        if len(word) < 2 or (not keep_stopwords and word in STOPWORDS):
            continue
        out.append(_stem(word))
    return out


def query_words(text: str, *, limit: int = 32) -> list[str]:
    """Distinct meaningful words of a search query, for an OR full-text query.

    Only ``\\w`` characters survive, so the result can be joined into a
    ``websearch_to_tsquery`` string without any operator injection.
    """
    # No Arabic folding here: Postgres indexes the stored text as written.
    plain = unicodedata.normalize("NFKC", text).casefold()
    seen: list[str] = []
    for word in _WORD_RE.findall(plain):
        word = word.replace("_", "")
        if len(word) < 2 or word in STOPWORDS or word in seen:
            continue
        seen.append(word)
        if len(seen) >= limit:
            break
    return seen
