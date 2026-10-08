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

Key rotation (versioned keys, see ``pairly_core.crypto.keys``):

* writes use the current key: ``blind_index(...)``;
* lookups match every configured key: ``blind_index_candidates(...)`` (dual lookup),
  used as ``column.in_(candidates)``, so a row written under the old or the new key is
  found during a rotation;
* ``blind_index_all(...)`` gives the index under each version, for code that stores
  more than one (dual write);
* ``pairly-admin crypto rotate-blind-index`` recomputes stored indexes under the
  current key from the encrypted source column (``crypto.blind_index_rotation``).

A blind index column declares its source with ``info=blind_index_info(kind, source)``,
so the rotation finds every index column without a registry to keep in sync. The
stored value stays a bare 64-character hex digest (no version prefix): the version is
not needed because lookups try every configured key and the rotation recomputes from
the plaintext.

The rollout that never misses a lookup: (1) add ``PAIRLY_BLIND_INDEX_KEY_2`` and
restart everything with the current version unchanged (every process now looks up
under both keys); (2) set ``PAIRLY_BLIND_INDEX_KEY_CURRENT=2`` and restart (new
indexes use key 2, lookups still try both); (3) run the rotate command until
``--dry-run`` reports nothing left; (4) remove the old key.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import unicodedata
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy import MetaData, Table

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


def _message(kind: str, value: str | None, org_id: uuid.UUID) -> bytes | None:
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
    return b"|".join((_TAG, kind.encode("ascii"), org_id.bytes, normalized.encode("utf-8")))


def blind_index(
    kind: IndexKind,
    value: str | None,
    org_id: uuid.UUID,
    keyring: KeyRing | None = None,
    *,
    version: str | None = None,
) -> str | None:
    """Hex HMAC-SHA256 of the normalised value, or ``None`` when it does not normalise.

    Uses the current blind index key, or the key ``version`` when given (raises
    ``UnknownKeyVersionError`` when that version is not configured).
    """
    message = _message(kind, value, org_id)
    if message is None:
        return None
    ring = keyring or get_keyring()
    key = ring.blind_index_key if version is None else ring.blind_key(version)
    return hmac.new(key, message, hashlib.sha256).hexdigest()


def blind_index_all(
    kind: IndexKind, value: str | None, org_id: uuid.UUID, keyring: KeyRing | None = None
) -> dict[str, str]:
    """The index under every configured key version (empty when it does not normalise)."""
    message = _message(kind, value, org_id)
    if message is None:
        return {}
    ring = keyring or get_keyring()
    return {
        version: hmac.new(ring.blind_key(version), message, hashlib.sha256).hexdigest()
        for version in ring.blind_index_versions
    }


def blind_index_candidates(
    kind: IndexKind, value: str | None, org_id: uuid.UUID, keyring: KeyRing | None = None
) -> tuple[str, ...]:
    """Every index a stored row may hold for ``value`` (current key first): for lookups."""
    return tuple(blind_index_all(kind, value, org_id, keyring).values())


# ------------------------------------------------------------------ index columns

INFO_KEY = "pairly_blind_index"


def blind_index_info(kind: IndexKind, source: str) -> dict[str, Any]:
    """``mapped_column(..., info=...)`` for a blind index of the encrypted ``source``."""
    if kind not in NORMALIZERS:
        raise ValueError(f"unknown blind index kind {kind!r}")
    return {INFO_KEY: {"kind": kind, "source": source}}


@dataclass(frozen=True, slots=True)
class BlindIndexColumn:
    table: Table
    column: str
    source: str
    kind: IndexKind


def blind_index_columns(metadata: MetaData) -> Iterator[BlindIndexColumn]:
    """Every declared blind index column in ``metadata``, sorted by table and column."""
    from pairly_core.crypto.types import _EncryptedType

    for table in sorted(metadata.tables.values(), key=lambda t: t.name):
        for col in sorted(table.columns, key=lambda c: c.name):
            spec = col.info.get(INFO_KEY)
            if spec is None:
                continue
            source = spec["source"]
            if source not in table.c or not isinstance(table.c[source].type, _EncryptedType):
                raise ValueError(f"{table.name}.{col.name}: source {source!r} is not encrypted")
            if "id" not in table.c or "org_id" not in table.c:
                raise ValueError(f"{table.name} has a blind index but no id or org_id")
            yield BlindIndexColumn(table, col.name, source, spec["kind"])
