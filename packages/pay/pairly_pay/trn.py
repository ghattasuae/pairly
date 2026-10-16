# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""UAE Tax Registration Numbers (TRN): format check and the verification hook.

A UAE TRN is 15 digits (brief 4.1.2). ``normalize_trn`` drops spaces and dashes (TRNs
are often printed in groups); ``check_trn_format`` says whether what is left is 15
digits. It never says a TRN is real: only an official source can.

``TrnVerifier`` is the hook for that source. The only implementation today is
``NotVerifiedTrnVerifier``: it answers ``not_verified`` for every TRN, so nothing ever
reads as officially verified. TODO: source of truth (the Federal Tax Authority's TRN
verification, once access and terms are settled; ASK before connecting anything).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final, Literal, Protocol, runtime_checkable

TRN_LENGTH: Final = 15
MAX_INPUT: Final = 40
_STRIP: Final = re.compile(r"[\s\-\u00a0\u200b-\u200f\u2060\ufeff]")
_DIGITS: Final = re.compile(r"^\d{15}$")
# Arabic-Indic and extended Arabic-Indic digits, as typed on Arabic keyboards.
_ARABIC_DIGITS: Final = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")

TrnFormatError = Literal["empty", "not_digits", "length"]
TrnStatus = Literal["verified", "not_verified", "invalid", "error"]


@dataclass(frozen=True, slots=True)
class TrnFormat:
    trn: str
    error: TrnFormatError | None = None

    @property
    def valid(self) -> bool:
        return self.error is None


def normalize_trn(text: str) -> str:
    return _STRIP.sub("", (text or "")[:MAX_INPUT]).translate(_ARABIC_DIGITS)


def check_trn_format(text: str) -> TrnFormat:
    trn = normalize_trn(text)
    if not trn:
        return TrnFormat(trn=trn, error="empty")
    if not trn.isascii() or not trn.isdigit():
        return TrnFormat(trn=trn, error="not_digits")
    if not _DIGITS.fullmatch(trn):
        return TrnFormat(trn=trn, error="length")
    return TrnFormat(trn=trn)


@dataclass(frozen=True, slots=True)
class TrnVerification:
    """What a verifier said. ``source`` names it; ``reason`` explains a non-answer."""

    trn: str
    status: TrnStatus
    source: str
    reason: str | None = None


@runtime_checkable
class TrnVerifier(Protocol):
    """Checks a well-formed TRN against an official source."""

    @property
    def source(self) -> str: ...

    async def verify(self, trn: str) -> TrnVerification: ...


@dataclass(frozen=True, slots=True)
class NotVerifiedTrnVerifier:
    """The default: no official source is connected, so nothing is ever verified.

    TODO: source of truth. Replace with a verifier for the Federal Tax Authority's TRN
    check once there is an agreed way to call it. Until then the vendor shows the TRN
    as "format checked, not verified" and a person decides.
    """

    source: str = "none"

    async def verify(self, trn: str) -> TrnVerification:
        fmt = check_trn_format(trn)
        if not fmt.valid:
            return TrnVerification(trn=fmt.trn, status="invalid", source=self.source)
        return TrnVerification(
            trn=fmt.trn,
            status="not_verified",
            source=self.source,
            reason="no_official_source_configured",
        )


DEFAULT_VERIFIER: Final[TrnVerifier] = NotVerifiedTrnVerifier()
