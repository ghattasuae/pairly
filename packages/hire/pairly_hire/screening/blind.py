# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Blind mode (brief 3.3.3): what the scoring model and reviewers do not see.

On by default, per organisation (``pairly_hire.screening.models.HireScreeningSettings``).
It hides, from the text the scoring model reads and from what reviewers see until the
shortlist stage:

* the name (every name the application and the parsed CV give, in full and word by
  word), and the contact details that carry it (email, phone, web links);
* gender markers (a gender line, titles such as Mr or Ms, gendered pronouns);
* age and date of birth (labelled lines, "34 years old", "born in ...");
* nationality (a nationality, citizenship or place of birth line, and that value
  wherever else it appears, "Indian passport" and the like);
* marital status (a labelled line, "married with two children");
* the home address (an address line, and the line that gives the home city and
  country);
* university names (from the parsed CV and by pattern), unless the organisation
  turns that part off (``universities=False``);
* a religion line, when a CV has one.

Photos never reach the scoring model: it reads text only, and reviewers see the
redacted text (never the file) while the screening is blind.

How it works: ``blind(guarded, sources, config)`` runs on the guardrails' sanitised
text (``GuardedInput``: hidden text and instructions already removed) and replaces
each hidden span with a placeholder such as ``[name]``. ``BlindedInput`` keeps an
offset map from its text to the sanitised text, so evidence is still located in the
original CV through the guardrails' own map. A quote that touches a placeholder maps
to nothing, so blind-redacted text can never be evidence (no credit).

Deterministic and conservative: it would rather hide a word too many (a company named
after the candidate's family name) than leave a protected marker in. The evals
(``evals/hire``) measure that every protected marker of the fairness pairs is gone.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Final

from pairly_core.extraction.guarded import GuardedInput
from pairly_core.guardrails.report import text_digest
from pairly_core.guardrails.sanitize import DATA_NOTE, DATA_TAG
from pairly_hire.screening.profile import ParsedProfile


class BlindCategory(StrEnum):
    NAME = "name"
    CONTACT = "contact"
    GENDER = "gender"
    AGE = "age"
    NATIONALITY = "nationality"
    MARITAL_STATUS = "marital_status"
    ADDRESS = "address"
    UNIVERSITY = "university"
    RELIGION = "religion"


PLACEHOLDER: Final[dict[BlindCategory, str]] = {
    BlindCategory.NAME: "[name]",
    BlindCategory.CONTACT: "[contact]",
    BlindCategory.GENDER: "[gender]",
    BlindCategory.AGE: "[age]",
    BlindCategory.NATIONALITY: "[nationality]",
    BlindCategory.MARITAL_STATUS: "[marital status]",
    BlindCategory.ADDRESS: "[address]",
    BlindCategory.UNIVERSITY: "[university]",
    BlindCategory.RELIGION: "[religion]",
}
BLIND_NOTE: Final = (
    "Text in square brackets such as [name] or [university] was hidden for fair "
    "screening. Never guess it and never use it."
)


@dataclass(frozen=True, slots=True)
class BlindConfig:
    """Per organisation. ``enabled=False`` turns blind mode off (scoring sees names)."""

    enabled: bool = True
    universities: bool = True

    def as_dict(self) -> dict[str, bool]:
        return {"enabled": self.enabled, "universities": self.universities}


@dataclass(frozen=True, slots=True)
class BlindSources:
    """What is known about the person, from the application form and the parsed CV."""

    names: tuple[str, ...] = ()
    emails: tuple[str, ...] = ()
    phones: tuple[str, ...] = ()
    links: tuple[str, ...] = ()
    locations: tuple[str, ...] = ()
    institutions: tuple[str, ...] = ()

    @classmethod
    def build(
        cls, contact: Mapping[str, Any] | None, profile: ParsedProfile | None
    ) -> BlindSources:
        contact = contact or {}

        def text(value: Any) -> list[str]:
            return [value.strip()] if isinstance(value, str) and value.strip() else []

        names = text(contact.get("full_name"))
        emails = text(contact.get("email"))
        phones = text(contact.get("phone"))
        links = text(contact.get("profile_url"))
        locations = text(contact.get("location"))
        institutions: list[str] = []
        if profile is not None:
            for item in (profile.name, profile.other_name):
                if item is not None:
                    names.append(item.value)
            if profile.email is not None:
                emails.append(profile.email.value)
            if profile.phone is not None:
                phones.append(profile.phone.value)
            links += [link.value for link in profile.links]
            for item in (profile.city, profile.country):
                if item is not None:
                    locations.append(item.value)
            institutions += [i.value for i in profile.institutions]
        return cls(
            names=_unique(names),
            emails=_unique(emails),
            phones=_unique(phones),
            links=_unique(links),
            locations=_unique(locations),
            institutions=_unique(institutions),
        )


def _unique(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(v for v in values if v))


@dataclass(frozen=True, slots=True)
class BlindSpan:
    """A hidden range of the sanitised text and what it hid."""

    start: int
    end: int
    category: BlindCategory


@dataclass(frozen=True, slots=True)
class _Segment:
    blinded_start: int
    sanitized_start: int
    length: int


@dataclass(frozen=True, slots=True, eq=False)
class BlindedInput:
    """The text the scoring model reads. Implements ``SanitizedInput`` (evidence)."""

    text: str
    nonce: str
    guarded: GuardedInput
    spans: tuple[BlindSpan, ...]
    config: BlindConfig
    _segments: tuple[_Segment, ...] = field(repr=False)

    @property
    def data_block(self) -> str:
        return (
            f"{DATA_NOTE} {BLIND_NOTE}\n"
            f'<{DATA_TAG} nonce="{self.nonce}">\n{self.text}\n</{DATA_TAG} nonce="{self.nonce}">'
        )

    @property
    def digest(self) -> str:
        return text_digest(self.text)

    def to_sanitized(self, start: int, end: int) -> tuple[int, int] | None:
        """The sanitised range of blinded ``[start, end)``, or ``None`` when any part of
        it is a placeholder (hidden text is never evidence)."""
        if not 0 <= start < end <= len(self.text):
            return None
        pieces: list[tuple[int, int]] = []
        covered = 0
        for seg in self._segments:
            lo = max(start, seg.blinded_start)
            hi = min(end, seg.blinded_start + seg.length)
            if lo < hi:
                s_lo = seg.sanitized_start + (lo - seg.blinded_start)
                pieces.append((s_lo, s_lo + (hi - lo)))
                covered += hi - lo
        if covered != end - start or len(pieces) != 1:
            return None
        return pieces[0]

    def to_original(self, start: int, end: int) -> tuple[int, int] | None:
        sanitized = self.to_sanitized(start, end)
        if sanitized is None:
            return None
        return self.guarded.to_original(*sanitized)

    def original_spans(self) -> list[tuple[int, int, BlindCategory]]:
        """Each hidden range in original document offsets (for the reviewer's view)."""
        out: list[tuple[int, int, BlindCategory]] = []
        offsets = self.guarded.model_input.offsets
        for span in self.spans:
            pieces = offsets.to_original(span.start, span.end)
            if pieces:
                out.append((pieces[0].start, pieces[-1].end, span.category))
        return out

    def categories(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for span in self.spans:
            counts[span.category.value] = counts.get(span.category.value, 0) + 1
        return dict(sorted(counts.items()))


def _nonce(text: str) -> str:
    salt = 0
    while True:
        digest = hashlib.sha256(
            b"pairly.hire.blind.nonce.v1\0" + str(salt).encode() + b"\0" + text.encode("utf-8")
        ).hexdigest()[:32]
        if digest not in text:
            return digest
        salt += 1


# ------------------------------------------------------------------ detectors

_LABELS: Final[tuple[tuple[BlindCategory, str], ...]] = (
    (BlindCategory.NATIONALITY, r"nationality|citizenship|place\s+of\s+birth"),
    (BlindCategory.GENDER, r"gender|sex"),
    (
        BlindCategory.AGE,
        r"date\s+of\s+birth|birth\s*date|d\.?\s?o\.?\s?b\.?|year\s+of\s+birth|born|age",
    ),
    (BlindCategory.MARITAL_STATUS, r"marital\s+status|civil\s+status|family\s+status"),
    (BlindCategory.RELIGION, r"religion|faith"),
    (BlindCategory.ADDRESS, r"(?:home\s+|postal\s+|current\s+)?address|residence"),
    (BlindCategory.NATIONALITY, r"الجنسية|مكان\s+الميلاد"),
    (BlindCategory.GENDER, r"الجنس|النوع"),
    (BlindCategory.AGE, r"تاريخ\s+الميلاد|سنة\s+الميلاد|العمر|السن|مواليد"),
    (BlindCategory.MARITAL_STATUS, r"الحالة\s+الاجتماعية"),
    (BlindCategory.RELIGION, r"الديانة|الدين"),
    (BlindCategory.ADDRESS, r"العنوان|مكان\s+الإقامة"),
)
_LABEL_RE: Final = re.compile(
    r"(?im)(?:^|(?<=[|•·;]))[^\S\n]*(?:[-*•·][^\S\n]*)?(?P<label>"
    + "|".join(f"(?P<l{i}>{pattern})" for i, (_, pattern) in enumerate(_LABELS))
    + r")[^\S\n]*[:\uff1a][^\S\n]*(?P<value>[^\n|•·;]*)"
)
_EMAIL_RE: Final = re.compile(r"[\w.+'-]+@[\w-]+(?:\.[\w-]+)+")
_URL_RE: Final = re.compile(
    r"(?:https?://|www\.)\S+"
    r"|(?<![\w.@-])[a-z0-9-]+(?:\.[a-z0-9-]+)+/\S*"
    r"|(?<![\w.@-])[a-z0-9-]+(?:\.[a-z0-9-]+)*\.(?:com|net|org|io|dev|me|ae|uk|co|info|example|test)"
    r"(?![\w.])"
)
_PHONE_RE: Final = re.compile(r"(?<![\w+])(?:\+|00)\d(?:[\s().-]?\d){7,14}(?!\d)")
_HONORIFIC_RE: Final = re.compile(r"\b(?:Mr|Mrs|Ms|Miss|Mx)\b\.?|السيدة|السيد")
_GENDER_WORD_RE: Final = re.compile(
    r"(?<!\w)(?:male|female|man|woman|he|she|him|her|his|hers|himself|herself)(?!\w)",
    re.IGNORECASE,
)
_AGE_RE: Final = re.compile(
    r"(?<!\w)\d{2}\s*(?:years?|yrs?)[\s-]*old(?!\w)"
    r"|(?<!\w)aged?\s*:?\s*\d{2}(?!\d)"
    r"|(?<!\w)born\s+(?:in|on)\s+[^\n,.;]{1,40}",
    re.IGNORECASE,
)
_MARITAL_RE: Final = re.compile(
    r"(?<!\w)(?:married|single|divorced|widowed)\s+with\s+\w+\s+(?:children|kids|child)(?!\w)",
    re.IGNORECASE,
)
DEMONYMS: Final = [
    "Afghan",
    "American",
    "Arab",
    "Australian",
    "Bahraini",
    "Bangladeshi",
    "British",
    "Canadian",
    "Chinese",
    "Egyptian",
    "Emirati",
    "Filipino",
    "French",
    "German",
    "Indian",
    "Indonesian",
    "Iranian",
    "Iraqi",
    "Irish",
    "Italian",
    "Jordanian",
    "Kenyan",
    "Kuwaiti",
    "Lebanese",
    "Libyan",
    "Moroccan",
    "Nepali",
    "Nigerian",
    "Omani",
    "Pakistani",
    "Palestinian",
    "Qatari",
    "Russian",
    "Saudi",
    "Somali",
    "Spanish",
    "Sri",
    "Lankan",
    "Sudanese",
    "Syrian",
    "Tunisian",
    "Turkish",
    "Ugandan",
    "Ukrainian",
    "Yemeni",
]
_DEMONYM_RE: Final = re.compile(
    r"(?<!\w)(?:" + "|".join([*DEMONYMS, "Sri Lankan"]) + r")"
    r"\s+(?:national|citizen|passport(?:\s+holder)?|nationality)(?!\w)",
    re.IGNORECASE,
)
_UNIVERSITY_RE: Final = re.compile(
    r"(?:\b(?:[A-Z][\w'&.-]*\s+){1,4}(?:University|College|Polytechnic|Academy|Institute)"
    r"(?:\s+of\s+(?:[A-Z][\w'&.-]*)(?:\s+[A-Z][\w'&.-]*){0,3})?)"
    r"|(?:\b(?:University|College|Institute|Polytechnic|Academy)\s+of\s+"
    r"(?:[A-Z][\w'&.-]*)(?:\s+(?:of\s+)?[A-Z][\w'&.-]*){0,3})"
    r"|(?:(?<!\w)(?:جامعة|كلية|معهد)(?:\s+[^\s،,؛;\d]+){1,3})"
)
# Parts of a line separated by a bar, a bullet, a tab or a wide space (contact rows).
_SEGMENT_RE: Final = re.compile(r"[^|•·\t\n]+")
_WIDE_SPACE_RE: Final = re.compile(r"\s{3,}")
_ADDRESS_START_RE: Final = re.compile(r"(?im)^[^\S\n]*(?:p\.?\s?o\.?\s+box|ص\.?\s?ب)[^\n]*")
# A languages line: a language level next to the word.
_LANGUAGE_LINE_RE: Final = re.compile(
    r"(?i)\b(?:native|fluent|professional|conversational|basic|beginner|intermediate|"
    r"advanced|mother\s+tongue)\b|اللغات|اللغة"
)
# Words a name part never hides on its own (particles shared by many names).
_NAME_STOP: Final = frozenset(
    {"al", "el", "bin", "ibn", "abu", "bint", "de", "da", "del", "der", "van", "von", "la", "le"}
)


def _word_re(phrase: str) -> re.Pattern[str]:
    parts = [re.escape(p) for p in phrase.split()]
    return re.compile(r"(?<!\w)" + r"[\s\-]+".join(parts) + r"(?!\w)", re.IGNORECASE)


def _name_re(phrase: str) -> re.Pattern[str]:
    """Like ``_word_re``, but a letter outside ASCII matches any one visible character:
    a PDF may drop or replace a letter its font cannot map ("Şafii" read as "?afii"),
    or a CV may write the name without accents. Arabic names are matched as written."""
    if any("\u0600" <= ch <= "\u06ff" for ch in phrase):
        return _word_re(phrase)
    parts = ["".join(re.escape(ch) if ch.isascii() else r"\S" for ch in p) for p in phrase.split()]
    return re.compile(r"(?<![^\W\x00])" + r"[\s\-]+".join(parts) + r"(?!\w)", re.IGNORECASE)


def _name_parts(name: str) -> list[str]:
    out: list[str] = []
    for part in re.split(r"[\s\-]+", name):
        clean = part.strip(".,'\u2019")
        if clean.casefold() in _NAME_STOP:
            continue
        arabic = any("؀" <= ch <= "ۿ" for ch in clean)
        if len(clean) >= (2 if arabic else 3):
            out.append(clean)
    return out


def _phone_re(phone: str) -> re.Pattern[str] | None:
    digits = re.sub(r"\D", "", phone)
    if len(digits) < 7:
        return None
    return re.compile(r"(?<!\d)" + r"[\s().+-]{0,3}".join(digits) + r"(?!\d)")


def _lines(text: str) -> Iterable[tuple[int, int]]:
    pos = 0
    for raw in text.split("\n"):
        yield pos, pos + len(raw)
        pos += len(raw) + 1


def _segments(text: str, start: int, end: int) -> Iterable[tuple[int, int]]:
    for piece in _SEGMENT_RE.finditer(text, start, end):
        position = piece.start()
        for gap in _WIDE_SPACE_RE.finditer(text, piece.start(), piece.end()):
            yield position, gap.start()
            position = gap.end()
        yield position, piece.end()


def _line_of(text: str, position: int) -> str:
    start = text.rfind("\n", 0, position) + 1
    end = text.find("\n", position)
    return text[start : end if end >= 0 else len(text)]


def _find(pattern: re.Pattern[str], text: str, category: BlindCategory) -> list[BlindSpan]:
    return [
        BlindSpan(m.start(), m.end(), category)
        for m in pattern.finditer(text)
        if m.end() > m.start()
    ]


def detect(text: str, sources: BlindSources, config: BlindConfig) -> list[BlindSpan]:
    """Every range of ``text`` to hide (unmerged, may overlap)."""
    found: list[BlindSpan] = []
    labelled_values: list[tuple[BlindCategory, str]] = []
    for match in _LABEL_RE.finditer(text):
        index = next(i for i in range(len(_LABELS)) if match.group(f"l{i}"))
        category = _LABELS[index][0]
        end = match.end("value")
        # Keep trailing spaces out of the span.
        while end > match.start("label") and text[end - 1].isspace():
            end -= 1
        found.append(BlindSpan(match.start("label"), end, category))
        value = match.group("value").strip()
        if category is BlindCategory.NATIONALITY and value and len(value.split()) <= 3:
            labelled_values.append((category, value))
    # Names: in full first, then each part of the name on its own.
    for name in sources.names:
        found += _find(_name_re(name), text, BlindCategory.NAME)
        for part in _name_parts(name):
            found += _find(_name_re(part), text, BlindCategory.NAME)
    # Contact details.
    found += _find(_EMAIL_RE, text, BlindCategory.CONTACT)
    found += _find(_URL_RE, text, BlindCategory.CONTACT)
    found += _find(_PHONE_RE, text, BlindCategory.CONTACT)
    for email in sources.emails:
        found += _find(re.compile(re.escape(email), re.IGNORECASE), text, BlindCategory.CONTACT)
    for link in sources.links:
        bare = re.sub(r"^https?://", "", link).rstrip("/")
        if len(bare) >= 6:
            found += _find(re.compile(re.escape(bare), re.IGNORECASE), text, BlindCategory.CONTACT)
    for phone in sources.phones:
        pattern = _phone_re(phone)
        if pattern is not None:
            found += _find(pattern, text, BlindCategory.CONTACT)
    # Gender, age, marital status, nationality wherever they show.
    found += _find(_HONORIFIC_RE, text, BlindCategory.GENDER)
    found += _find(_GENDER_WORD_RE, text, BlindCategory.GENDER)
    found += _find(_AGE_RE, text, BlindCategory.AGE)
    found += _find(_MARITAL_RE, text, BlindCategory.MARITAL_STATUS)
    found += _find(_DEMONYM_RE, text, BlindCategory.NATIONALITY)
    for category, value in labelled_values:
        # The same word as a language ("Spanish (Native)") is a skill, not a nationality.
        found += [
            span
            for span in _find(_word_re(value), text, category)
            if not _LANGUAGE_LINE_RE.search(_line_of(text, span.start))
        ]
    # The home address: an address line, and the line that names the home city.
    found += _find(_ADDRESS_START_RE, text, BlindCategory.ADDRESS)
    homes = {loc.casefold() for loc in sources.locations}
    for start, end in _lines(text):
        for seg_start, seg_end in _segments(text, start, end):
            segment = text[seg_start:seg_end]
            stripped = segment.strip()
            if not stripped or len(stripped) > 80:
                continue
            parts = [p.strip().casefold() for p in re.split(r"[,،]", stripped)]
            if len(parts) <= 3 and (parts[0] in homes or stripped.casefold() in homes):
                lead = seg_start + len(segment) - len(segment.lstrip())
                found.append(BlindSpan(lead, lead + len(stripped), BlindCategory.ADDRESS))
    # University names (configurable per organisation).
    if config.universities:
        for institution in sources.institutions:
            found += _find(_word_re(institution), text, BlindCategory.UNIVERSITY)
        found += _find(_UNIVERSITY_RE, text, BlindCategory.UNIVERSITY)
    return found


def merge(spans: Sequence[BlindSpan]) -> tuple[BlindSpan, ...]:
    """Merge overlapping or touching spans; the first span's category names the result."""
    merged: list[BlindSpan] = []
    for span in sorted(spans, key=lambda s: (s.start, -s.end)):
        if merged and span.start <= merged[-1].end:
            last = merged[-1]
            if span.end > last.end:
                merged[-1] = BlindSpan(last.start, span.end, last.category)
            continue
        merged.append(span)
    return tuple(merged)


def blind(guarded: GuardedInput, sources: BlindSources, config: BlindConfig) -> BlindedInput:
    """The blinded model input for ``guarded`` (see the module docstring)."""
    text = guarded.text
    spans = merge(detect(text, sources, config)) if config.enabled else ()
    pieces: list[str] = []
    segments: list[_Segment] = []
    position = 0
    produced = 0
    for span in spans:
        if span.start > position:
            chunk = text[position : span.start]
            segments.append(_Segment(produced, position, len(chunk)))
            pieces.append(chunk)
            produced += len(chunk)
        placeholder = PLACEHOLDER[span.category]
        pieces.append(placeholder)
        produced += len(placeholder)
        position = span.end
    if position < len(text):
        chunk = text[position:]
        segments.append(_Segment(produced, position, len(chunk)))
        pieces.append(chunk)
    blinded = "".join(pieces)
    return BlindedInput(
        text=blinded,
        nonce=_nonce(blinded),
        guarded=guarded,
        spans=spans,
        config=config,
        _segments=tuple(segments),
    )


def redact_text(text: str, sources: BlindSources, config: BlindConfig) -> str:
    """``text`` with every blind span replaced (for snippets shown while blind, such as
    a guardrail finding's evidence). Unchanged when blind mode is off."""
    if not config.enabled or not text:
        return text
    out: list[str] = []
    position = 0
    for span in merge(detect(text, sources, config)):
        out.append(text[position : span.start])
        out.append(PLACEHOLDER[span.category])
        position = span.end
    out.append(text[position:])
    return "".join(out)


# ------------------------------------------------------------------ reviewer view


@dataclass(frozen=True, slots=True)
class DisplayText:
    """The original CV text with the blind spans replaced by placeholders, for
    reviewers while the screening is blind, and a map from original offsets to it."""

    text: str
    _replacements: tuple[tuple[int, int, int, int], ...]  # orig start, end, disp start, len

    def offset(self, original: int) -> int:
        shift = 0
        for o_start, o_end, _, length in self._replacements:
            if original >= o_end:
                shift += length - (o_end - o_start)
            elif original > o_start:
                return o_start + shift
        return original + shift

    def range(self, start: int, end: int) -> tuple[int, int]:
        return self.offset(start), self.offset(end)


def display_text(original: str, spans: Sequence[tuple[int, int, BlindCategory]]) -> DisplayText:
    ordered = sorted(spans)
    merged: list[tuple[int, int, BlindCategory]] = []
    for start, end, category in ordered:
        if merged and start <= merged[-1][1]:
            last = merged[-1]
            merged[-1] = (last[0], max(last[1], end), last[2])
        else:
            merged.append((start, end, category))
    out: list[str] = []
    replacements: list[tuple[int, int, int, int]] = []
    position = 0
    produced = 0
    for start, end, category in merged:
        out.append(original[position:start])
        produced += start - position
        placeholder = PLACEHOLDER[category]
        replacements.append((start, end, produced, len(placeholder)))
        out.append(placeholder)
        produced += len(placeholder)
        position = end
    out.append(original[position:])
    return DisplayText("".join(out), tuple(replacements))


# ------------------------------------------------------------------ known identity


def merge_sources(*many: BlindSources) -> BlindSources:
    """Every value of several sources, each once."""
    return BlindSources(
        names=_unique(v for s in many for v in s.names),
        emails=_unique(v for s in many for v in s.emails),
        phones=_unique(v for s in many for v in s.phones),
        links=_unique(v for s in many for v in s.links),
        locations=_unique(v for s in many for v in s.locations),
        institutions=_unique(v for s in many for v in s.institutions),
    )


def identity_spans(text: str, sources: BlindSources) -> list[tuple[int, int, str]]:
    """Where ``text`` names the known person: ``(start, end, kind)`` with kind ``name``,
    ``email`` or ``phone``, merged, in order. Only the values ``sources`` knows (the
    exact email, the phone's digits, the name in full and word by word), so it can mask
    a message that must otherwise read exactly as sent."""
    found: list[tuple[int, int, str]] = []
    for email in sources.emails:
        pattern = re.compile(re.escape(email), re.IGNORECASE)
        found += [(m.start(), m.end(), "email") for m in pattern.finditer(text)]
    for phone in sources.phones:
        phone_pattern = _phone_re(phone)
        if phone_pattern is not None:
            for m in phone_pattern.finditer(text):
                # The pattern starts at the first digit: keep a leading "+" with it.
                start = m.start() - 1 if m.start() > 0 and text[m.start() - 1] == "+" else m.start()
                found.append((start, m.end(), "phone"))
    for name in sources.names:
        for pattern in (_name_re(name), *(_name_re(p) for p in _name_parts(name))):
            found += [
                (m.start(), m.end(), "name") for m in pattern.finditer(text) if m.end() > m.start()
            ]
    merged: list[tuple[int, int, str]] = []
    for start, end, kind in sorted(found, key=lambda s: (s[0], -s[1])):
        if merged and start < merged[-1][1]:
            last = merged[-1]
            merged[-1] = (last[0], max(last[1], end), last[2])
            continue
        merged.append((start, end, kind))
    return merged
