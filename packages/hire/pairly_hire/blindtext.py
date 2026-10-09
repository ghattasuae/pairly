# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Blind mode for free text and for an approver's view of a message.

Blind mode (brief 3.3.3) hides who a candidate is from reviewers until the application
is revealed (``pairly_hire.reveal``). Structured fields are left out while blind; text
people or templates wrote can still name the candidate. Two helpers cover that text:

* ``redact_free_text``: notes, stage move reasons, timeline and message previews, and
  custom form answers, shown to reviewers while blind, go through the screening's blind
  redactor (``screening.blind.redact_text``) with the candidate's known identity: every
  name the application, the candidate row and the parsed CV give (in full and word by
  word), the email addresses, the phone numbers, the profile links and the home
  location, plus the redactor's own patterns (gender markers, age, nationality...).
* ``mask_identity``: the approver's view of a sensitive message (a rejection or an
  offer) while blind. The text must read exactly as it will be sent, so only the known
  name, email and phone are replaced, each by a labelled placeholder such as
  ``[name hidden in blind mode]``. The approval still binds the hash of the real
  message, and the email that is sent carries the real values.

``identity_sources`` gathers what is known about the person (pure: the caller passes
the decrypted values).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping
from typing import Any, Final

from pairly_hire.screening.blind import (
    PLACEHOLDER,
    BlindCategory,
    BlindConfig,
    BlindSources,
    identity_spans,
    merge_sources,
    redact_text,
)
from pairly_hire.screening.profile import ParsedProfile

NAME_HIDDEN: Final = "[name hidden in blind mode]"
EMAIL_HIDDEN: Final = "[email hidden in blind mode]"
PHONE_HIDDEN: Final = "[phone hidden in blind mode]"
PLACEHOLDERS: Final[dict[str, str]] = {
    "name": NAME_HIDDEN,
    "email": EMAIL_HIDDEN,
    "phone": PHONE_HIDDEN,
}
MASK_NOTE: Final = (
    "The candidate's name, email and phone are hidden in blind mode. "
    "The message is sent with the real details."
)

Redactor = Callable[[str], str]


def _text(value: Any) -> list[str]:
    return [value.strip()] if isinstance(value, str) and value.strip() else []


def identity_sources(
    *contacts: Mapping[str, Any] | None,
    profile: ParsedProfile | None = None,
) -> BlindSources:
    """Every known name, email, phone, link and location of the candidate: from each
    contact mapping (``full_name``, ``email``, ``phone``, ``location``,
    ``profile_url``) and the parsed CV profile."""
    parts = [BlindSources.build(c, None) for c in contacts if c]
    if profile is not None:
        parts.append(BlindSources.build(None, profile))
    return merge_sources(*parts) if parts else BlindSources()


def redact_free_text(text: str, sources: BlindSources, *, universities: bool = True) -> str:
    """``text`` with the candidate's identity and protected markers replaced
    (``[name]``, ``[contact]``...). Always on: call it only while blind. In free text
    the candidate's known home location is an address wherever it shows (in a CV the
    redactor hides it only on the line that gives the home address)."""
    out = redact_text(text, sources, BlindConfig(enabled=True, universities=universities))
    for location in sorted(sources.locations, key=len, reverse=True):
        for part in {location, *(p.strip() for p in re.split(r"[,،]", location))}:
            if len(part) >= 3:
                pattern = r"(?<!\w)" + r"\s+".join(map(re.escape, part.split())) + r"(?!\w)"
                out = re.sub(pattern, PLACEHOLDER[BlindCategory.ADDRESS], out, flags=re.I)
    return out


def redactor(sources: BlindSources, *, universities: bool = True) -> Redactor:
    def run(text: str) -> str:
        return redact_free_text(text, sources, universities=universities) if text else text

    return run


def mask_identity(text: str, sources: BlindSources) -> str:
    """``text`` with the known name, email and phone replaced by labelled placeholders
    (see the module docstring); everything else exactly as written."""
    if not text:
        return text
    out: list[str] = []
    position = 0
    for start, end, kind in identity_spans(text, sources):
        out.append(text[position:start])
        out.append(PLACEHOLDERS[kind])
        position = end
    out.append(text[position:])
    return "".join(out)


def mask_values(value: Any, sources: BlindSources, keys: Iterable[str] | None = None) -> Any:
    """``mask_identity`` on every string of ``value`` (a JSON-like structure); with
    ``keys``, only on those keys of a top-level mapping."""
    if isinstance(value, str):
        return mask_identity(value, sources)
    if isinstance(value, Mapping):
        wanted = set(keys) if keys is not None else None
        return {
            k: (mask_values(v, sources) if wanted is None or k in wanted else v)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [mask_values(v, sources) for v in value]
    return value


def redact_values(value: Any, run: Redactor) -> Any:
    """``run`` on every string of a JSON-like structure (custom form answers)."""
    if isinstance(value, str):
        return run(value)
    if isinstance(value, Mapping):
        return {k: redact_values(v, run) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_values(v, run) for v in value]
    return value
