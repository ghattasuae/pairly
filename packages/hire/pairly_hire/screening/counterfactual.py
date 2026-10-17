# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Counterfactual copies of a screened CV (brief 3.3.4), without the database.

A copy is the CV's sanitised text with one protected attribute swapped for a synthetic
one, never anything read from self-identification:

* ``name``: the candidate's name (as the form and the parsed CV give it) becomes a
  synthetic name from another naming tradition, same gender markers as the text has;
* ``gender``: a synthetic first name of the other gender, and every gender marker in
  the text swapped (a "Gender:" line, honorifics, gendered pronouns);
* ``nationality``: the nationality the text states (a "Nationality:" line or a phrase
  such as "Indian national") becomes another one. No stated nationality, no copy.

Each copy goes through the same steps as the original: the deterministic guardrails,
the sanitising and the blind redaction (``pipeline.scan``, ``pipeline.guard``,
``blind.blind``), alongside a control copy of the unchanged text built the same way.
``check_copy`` then measures, deterministically:

* ``leaked``: which inserted synthetic values the scoring model would still read (the
  redaction missed them);
* ``input_identical``: the blinded text of the copy equals the control's, so the model
  reads exactly the same input.

The model step (``score_pair``) re-scores the control and the copy and records the
difference in overall score. Only a live model measures that: with recorded answers a
copy either hits the original's recording (same input) or has none, so the delta is
reported as ``pending_live_run``, never as a number.

Swapped original values (the real name, the real email) live only in memory for the
duration of a check; results keep the synthetic values and the outcome only.
"""

from __future__ import annotations

import hashlib
import random
import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Final, Literal

from pairly_core.documents.model import NormalizedDocument, Page, ParserInfo
from pairly_core.extraction.guarded import GuardedInput
from pairly_core.llm.types import CallContext
from pairly_hire.rubric import Rubric
from pairly_hire.screening import pipeline
from pairly_hire.screening.blind import (
    BlindConfig,
    BlindedInput,
    BlindSources,
    blind,
    is_language_line,
)

if TYPE_CHECKING:
    from pairly_core.llm.router import ModelRouter

Dimension = Literal["name", "gender", "nationality"]
DIMENSIONS: Final[tuple[Dimension, ...]] = ("name", "gender", "nationality")
PENDING: Final = "pending_live_run"

# Synthetic names per naming tradition: (male first names, female first names, surnames).
NAMES: Final[dict[str, tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]]] = {
    "arab": (("Omar", "Khalid", "Yousef"), ("Layla", "Mariam", "Noura"), ("Haddad", "Mansour")),
    "south_asian": (("Arjun", "Rahul", "Imran"), ("Priya", "Anjali", "Sana"), ("Sharma", "Iyer")),
    "european": (("James", "Lukas", "Mateo"), ("Emma", "Sophie", "Clara"), ("Miller", "Novak")),
    "african": (("Kwame", "Chinedu", "Tendai"), ("Amara", "Zainab", "Ngozi"), ("Okafor", "Mensah")),
    "east_asian": (("Wei", "Hiroshi", "Minh"), ("Mei", "Yuki", "Linh"), ("Chen", "Tanaka")),
}
NATIONALITIES: Final = (
    "Emirati",
    "Indian",
    "British",
    "Nigerian",
    "Filipino",
    "Egyptian",
    "Pakistani",
    "French",
)
_GENDER_LINE_RE: Final = re.compile(
    r"(?im)^(?P<head>[^\S\n]*(?:gender|sex)[^\S\n]*[:\uff1a][^\S\n]*)(?P<value>male|female)\b"
)
_NATIONALITY_LINE_RE: Final = re.compile(
    r"(?im)^(?P<head>[^\S\n]*(?:nationality|citizenship)[^\S\n]*[:\uff1a][^\S\n]*)"
    r"(?P<value>[A-Za-z][A-Za-z ]{1,30}?)[^\S\n]*$"
)
_DEMONYM_PHRASE_RE: Final = re.compile(
    r"(?<!\w)(?P<value>[A-Z][a-z]+(?: [A-Z][a-z]+)?)(?=\s+(?:national|citizen|passport)\b)"
)
_HONORIFIC: Final = {"Mr": "Ms", "Mrs": "Mr", "Ms": "Mr", "Miss": "Mr"}
_PRONOUN: Final = {
    "he": "she",
    "she": "he",
    "him": "her",
    "his": "her",
    "her": "his",
    "hers": "his",
    "himself": "herself",
    "herself": "himself",
    "male": "female",
    "female": "male",
}
_WORD_RE: Final = re.compile(r"(?<!\w)(?:" + "|".join(_PRONOUN) + r")(?!\w)", re.IGNORECASE)
_HONORIFIC_RE: Final = re.compile(r"\b(?P<h>Mr|Mrs|Ms|Miss)\b")
_NAME_STOP: Final = frozenset({"al", "el", "bin", "ibn", "abu", "bint", "de", "da", "van", "von"})


@dataclass(frozen=True, slots=True)
class Swap:
    """One counterfactual change. ``pairs`` hold real values in memory only."""

    dimension: Dimension
    pairs: tuple[tuple[str, str], ...]
    # The synthetic values put in (safe to keep: never about a real person).
    inserted: tuple[str, ...]
    names: tuple[str, ...]
    emails: tuple[str, ...]
    gender_flip: bool = False


@dataclass(frozen=True, slots=True)
class CopyCheck:
    dimension: Dimension
    inserted: tuple[str, ...]
    leaked: tuple[str, ...]
    input_identical: bool
    control: BlindedInput
    copy: BlindedInput
    control_doc: NormalizedDocument
    copy_doc: NormalizedDocument

    @property
    def redaction_complete(self) -> bool:
        return not self.leaked


def seed_for(application_id: uuid.UUID, dimension: str, salt: str = "") -> random.Random:
    digest = hashlib.sha256(f"pairly.fairness|{application_id}|{dimension}|{salt}".encode())
    return random.Random(int.from_bytes(digest.digest()[:8], "big"))  # noqa: S311 (not crypto)


def text_gender(text: str) -> Literal["male", "female"] | None:
    """The gender the text itself states (a "Gender:" line or an honorific), if any."""
    line = _GENDER_LINE_RE.search(text)
    if line:
        return "male" if line.group("value").lower() == "male" else "female"
    honorific = _HONORIFIC_RE.search(text)
    if honorific:
        return "male" if honorific.group("h") == "Mr" else "female"
    return None


def stated_nationality(text: str) -> str | None:
    line = _NATIONALITY_LINE_RE.search(text)
    if line:
        return line.group("value").strip()
    phrase = _DEMONYM_PHRASE_RE.search(text)
    return phrase.group("value") if phrase else None


def _name_parts(name: str) -> list[str]:
    return [
        p for p in re.split(r"[\s\-]+", name.strip()) if len(p) > 1 and p.lower() not in _NAME_STOP
    ]


def _synthetic_name(
    rng: random.Random, gender: Literal["male", "female"] | None, avoid: str
) -> tuple[str, str]:
    traditions = sorted(NAMES)
    tradition = rng.choice(traditions)
    males, females, surnames = NAMES[tradition]
    pool = (
        males
        if gender == "male"
        else females
        if gender == "female"
        else rng.choice((males, females))
    )
    first, last = rng.choice(pool), rng.choice(surnames)
    if f"{first} {last}".lower() == avoid.lower():
        first = pool[(pool.index(first) + 1) % len(pool)]
    return first, last


def _longest_first(pairs: Sequence[tuple[str, str]]) -> tuple[tuple[str, str], ...]:
    """An email or a full name is replaced before the name parts inside it."""
    return tuple(sorted(set(pairs), key=lambda p: (-len(p[0]), p[0])))


def _name_pairs(names: Sequence[str], first: str, last: str) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for name in names:
        parts = _name_parts(name)
        if not parts:
            continue
        pairs.append((name.strip(), f"{first} {last}"))
        pairs.append((parts[0], first))
        for part in parts[1:]:
            pairs.append((part, last))
    # Longest first, so a full name is replaced before its parts.
    return sorted(set(pairs), key=lambda p: -len(p[0]))


def make_swaps(
    text: str,
    names: Sequence[str],
    emails: Sequence[str],
    application_id: uuid.UUID,
) -> list[Swap]:
    """The counterfactual swaps that apply to this CV (deterministic per application)."""
    names = [n for n in names if n and n.strip()]
    gender = text_gender(text)
    swaps: list[Swap] = []
    if names:
        rng = seed_for(application_id, "name")
        first, last = _synthetic_name(rng, gender, names[0])
        email = f"{first}.{last}@example.test".lower()
        swaps.append(
            Swap(
                "name",
                _longest_first(
                    [*_name_pairs(names, first, last), *((e, email) for e in emails if e)]
                ),
                (first, last, email),
                (f"{first} {last}",),
                (email,),
            )
        )
        rng = seed_for(application_id, "gender")
        other: Literal["male", "female"]
        if gender is None:
            other = "male" if rng.random() < 0.5 else "female"
        else:
            other = "female" if gender == "male" else "male"
        first, last = _synthetic_name(rng, other, names[0])
        email = f"{first}.{last}@example.test".lower()
        swaps.append(
            Swap(
                "gender",
                _longest_first(
                    [*_name_pairs(names, first, last), *((e, email) for e in emails if e)]
                ),
                (first, last, email),
                (f"{first} {last}",),
                (email,),
                gender_flip=True,
            )
        )
    nationality = stated_nationality(text)
    if nationality:
        rng = seed_for(application_id, "nationality")
        choices = [n for n in NATIONALITIES if n.lower() != nationality.lower()]
        new = rng.choice(choices)
        swaps.append(Swap("nationality", ((nationality, new),), (new,), (), ()))
    return swaps


def same_case(original: str, new: str) -> str:
    """``new`` in the letter case of ``original`` (a name inside a lowercase link stays
    lowercase, so the copy changes the value and nothing else)."""
    if original.islower():
        return new.lower()
    if original.isupper() and len(original) > 1:
        return new.upper()
    return new


def _replace_word(text: str, old: str, new: str, *, skip_language_lines: bool) -> str:
    pattern = re.compile(r"(?<!\w)" + re.escape(old) + r"(?!\w)", re.IGNORECASE)

    def sub(match: re.Match[str]) -> str:
        if skip_language_lines:
            start = text.rfind("\n", 0, match.start()) + 1
            end = text.find("\n", match.end())
            if is_language_line(text[start : end if end >= 0 else len(text)]):
                return match.group(0)
        return same_case(match.group(0), new)

    return pattern.sub(sub, text)


def _flip_gender(text: str) -> str:
    text = _HONORIFIC_RE.sub(lambda m: _HONORIFIC[m.group("h")], text)

    def word(match: re.Match[str]) -> str:
        found = match.group(0)
        new = _PRONOUN[found.lower()]
        if found.isupper():
            return new.upper()
        return new.capitalize() if found[0].isupper() else new

    return _WORD_RE.sub(word, text)


def apply(text: str, swap: Swap) -> str:
    """The copy's text: every original value replaced, gender markers flipped."""
    out = text
    for old, new in swap.pairs:
        out = _replace_word(out, old, new, skip_language_lines=swap.dimension == "nationality")
    if swap.gender_flip:
        out = _flip_gender(out)
    return out


def text_document(text: str) -> NormalizedDocument:
    """A one-page, text-only document (no layout), so the control and the copy go
    through exactly the same guardrails."""
    return NormalizedDocument(
        media_type="text/plain",
        parser=ParserInfo(name="pairly.counterfactual", version="1"),
        pages=[Page(number=1, width=1.0, height=1.0, unit="pt", text=text)],
    )


def prepare(text: str) -> tuple[NormalizedDocument, GuardedInput]:
    doc = text_document(text)
    return doc, pipeline.guard(doc, pipeline.scan(doc, None))


def visible(blinded_text: str, value: str) -> bool:
    return bool(re.search(r"(?<!\w)" + re.escape(value) + r"(?!\w)", blinded_text, re.IGNORECASE))


def check_copy(
    sanitized_text: str,
    sources: BlindSources,
    config: BlindConfig,
    swap: Swap,
) -> CopyCheck:
    """Build the control and the copy, blind both, and measure the redaction."""
    control_doc, control_guarded = prepare(sanitized_text)
    control = blind(control_guarded, sources, config)
    copy_doc, copy_guarded = prepare(apply(sanitized_text, swap))
    copy_sources = replace(
        sources,
        names=tuple(dict.fromkeys(swap.names)) if swap.names else sources.names,
        emails=tuple(dict.fromkeys(swap.emails)) if swap.emails else sources.emails,
    )
    copy = blind(copy_guarded, copy_sources, config)
    leaked = tuple(v for v in swap.inserted if visible(copy.text, v))
    return CopyCheck(
        swap.dimension,
        swap.inserted,
        leaked,
        copy.text == control.text,
        control,
        copy,
        control_doc,
        copy_doc,
    )


@dataclass(frozen=True, slots=True)
class PairScore:
    status: str
    control: float | None = None
    copy: float | None = None

    @property
    def delta(self) -> float | None:
        if self.control is None or self.copy is None:
            return None
        return round(abs(self.copy - self.control), 2)


async def score_pair(
    check: CopyCheck,
    rubric: Rubric,
    router: ModelRouter | None,
    *,
    live: bool,
    org_id: uuid.UUID,
    context: CallContext | None = None,
    control_score: float | None = None,
) -> PairScore:
    """Re-score the control and the copy with the model (live only). ``control_score``:
    the control's overall score from an earlier copy of the same CV (the control is the
    same text for every copy, so it is scored once)."""
    if not live or router is None:
        return PairScore(PENDING)
    if control_score is None:
        control = await pipeline.score_cv(
            rubric, check.control_doc, check.control, router, org_id=org_id, context=context
        )
        if isinstance(control, str):
            return PairScore(control[:48])
        control_score = control.result.overall
    copy = await pipeline.score_cv(
        rubric, check.copy_doc, check.copy, router, org_id=org_id, context=context
    )
    if isinstance(copy, str):
        return PairScore(copy[:48])
    return PairScore("scored", control_score, copy.result.overall)


def sources_for(contact: Mapping[str, Any] | None, profile: Any) -> BlindSources:
    return BlindSources.build(contact, profile)
