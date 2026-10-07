# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Blind indexes: keyed hashes of normalised values, for equality search and dedupe.

``blind_index(kind, value, org_id)`` is HMAC-SHA256 under ``PAIRLY_BLIND_INDEX_KEY``
(separate from every KEK) over the field kind, the organisation id and the normalised
value. Equal values in one organisation give equal indexes; the same value in two
organisations gives unrelated indexes, so indexes cannot be joined across tenants.

Normalisers make equal things compare equal:

* ``email``: trimmed, NFKC, lower case, IDNA (punycode) domain.
* ``phone``: E.164 (``+<digits>``). Local numbers get the default country code
  (UAE, ``+971``): ``050 123 4567`` and ``+971 50 123 4567`` index the same.
* ``iban``: spaces removed, upper case, and only when the ISO 13616 checksum holds.
* ``text``: NFKC, case folded, whitespace collapsed.

A value that does not normalise (not an email, too few digits, bad IBAN checksum)
has no index (``None``). The index is lossy on purpose: it reveals equality, nothing
else, and only to someone holding the key.

TODO(WP1.14 follow-up): blind index key rotation (needs a key version per index column).
"""

from __future__ import annotations

import hashlib
import hmac
import re
import unicodedata
import uuid
from collections.abc import Callable
from typing import Literal

from pairly_core.crypto.keys import KeyRing, get_keyring

IndexKind = Literal["email", "phone", "iban", "text"]
DEFAULT_COUNTRY_CODE = "971"
_TAG = b"pairly.bidx.v1"
_EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,253}$")
_DOMAIN_LABEL_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
_IBAN_RE = re.compile(r"^[A-Z]{2}[0-9]{2}[A-Z0-9]{11,30}$")
_INVISIBLE = dict.fromkeys([0x200B, 0x200C, 0x200D, 0x200E, 0x200F, 0x2060, 0xFEFF, 0x00AD], None)


def normalize_email(value: str) -> str | None:
    text = unicodedata.normalize("NFKC", value).translate(_INVISIBLE).strip().lower()
    text = text.removeprefix("mailto:")
    if not _EMAIL_RE.fullmatch(text):
        return None
    local, _, domain = text.rpartition("@")
    domain = domain.rstrip(".")
    try:
        ascii_domain = domain.encode("idna").decode("ascii")
    except UnicodeError:
        return None
    labels = ascii_domain.split(".")
    if len(labels) < 2 or not all(_DOMAIN_LABEL_RE.fullmatch(label) for label in labels):
        return None
    return f"{local}@{ascii_domain}"


def normalize_phone(value: str, default_country_code: str = DEFAULT_COUNTRY_CODE) -> str | None:
    """E.164. ``00`` becomes ``+``; a single leading ``0`` is a national trunk prefix."""
    text = unicodedata.normalize("NFKC", value).translate(_INVISIBLE).strip()
    text = re.sub(r"(?i)\s*(?:ext\.?|x)\s*\d+$", "", text)
    if re.search(r"[^\d\s()+./-]", text):
        return None
    plus = text.startswith("+")
    digits = re.sub(r"\D", "", text)
    if not plus and digits.startswith("00"):
        digits, plus = digits[2:], True
    if not plus:
        if digits.startswith("0"):
            digits = default_country_code + digits[1:]
        elif not digits.startswith(default_country_code):
            digits = default_country_code + digits
    # UAE numbers written as +971 0 50 ...: drop the trunk zero after the country code.
    if digits.startswith(default_country_code + "0"):
        digits = default_country_code + digits[len(default_country_code) + 1 :]
    if not 8 <= len(digits) <= 15 or digits.startswith("0"):
        return None
    return "+" + digits


def iban_checksum_ok(iban: str) -> bool:
    rearranged = iban[4:] + iban[:4]
    number = "".join(str(int(ch, 36)) for ch in rearranged)
    return int(number) % 97 == 1


def normalize_iban(value: str) -> str | None:
    text = re.sub(r"[\s-]", "", unicodedata.normalize("NFKC", value).translate(_INVISIBLE))
    text = text.upper()
    if not _IBAN_RE.fullmatch(text) or not iban_checksum_ok(text):
        return None
    return text


def normalize_text(value: str) -> str | None:
    text = unicodedata.normalize("NFKC", value).translate(_INVISIBLE).casefold()
    text = " ".join(text.split())
    return text or None


NORMALIZERS: dict[str, Callable[[str], str | None]] = {
    "email": normalize_email,
    "phone": normalize_phone,
    "iban": normalize_iban,
    "text": normalize_text,
}


def blind_index(
    kind: IndexKind, value: str | None, org_id: uuid.UUID, keyring: KeyRing | None = None
) -> str | None:
    """Hex HMAC-SHA256 of the normalised value, or ``None`` when it does not normalise."""
    if value is None:
        return None
    normalizer = NORMALIZERS.get(kind)
    if normalizer is None:
        raise ValueError(f"unknown blind index kind {kind!r}")
    if not isinstance(org_id, uuid.UUID):
        raise TypeError("org_id must be a UUID")
    normalized = normalizer(value)
    if normalized is None:
        return None
    ring = keyring or get_keyring()
    message = b"|".join((_TAG, kind.encode("ascii"), org_id.bytes, normalized.encode("utf-8")))
    return hmac.new(ring.blind_index_key, message, hashlib.sha256).hexdigest()
