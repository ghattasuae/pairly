# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""AES-256-GCM envelope encryption with per-record data keys.

Each value gets a fresh random 256-bit data key (DEK). The value is encrypted with the
DEK; the DEK is wrapped (AES-256-GCM) with the current key encryption key (KEK). Both
encryptions bind the same associated data (AAD): organisation id, table, column and row
id. A ciphertext copied to another row, column or tenant fails to decrypt, and so does
any change to a single byte.

Token format (ASCII, safe in a text column):

    v1:<kek-version>:<wrapped-dek>:<nonce>:<ciphertext>

``wrapped-dek`` is ``wrap-nonce || GCM(KEK, DEK)``; ``nonce`` and ``ciphertext`` (with
its tag) belong to the value. Binary parts are URL-safe base64 without padding. The KEK
version prefix lets ``pairly-admin crypto rotate`` find rows still on an old KEK and
re-wrap their data keys without touching the value ciphertext.
"""

from __future__ import annotations

import base64
import binascii
import os
import uuid
from dataclasses import dataclass

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from pairly_core.crypto.keys import VERSION_RE, KeyRing, UnknownKeyVersionError, get_keyring

TOKEN_VERSION = "v1"  # noqa: S105  (a format tag, not a secret)
NONCE_BYTES = 12
DEK_BYTES = 32
_AAD_TAG = b"pairly.aad.v1"
_WRAP_TAG = b"pairly.dek.v1"
_MAX_TOKEN_CHARS = 64 * 1024 * 1024


class DecryptionError(RuntimeError):
    """A ciphertext could not be decrypted: wrong context, tampered, or unknown key.

    The message never says which, so it cannot be used as an oracle.
    """


@dataclass(frozen=True, slots=True)
class Aad:
    """What a ciphertext is bound to. ``row_id`` is the row's primary key as text."""

    org_id: uuid.UUID
    table: str
    column: str
    row_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.org_id, uuid.UUID):
            raise TypeError("org_id must be a UUID")
        for name in ("table", "column", "row_id"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name):
                raise ValueError(f"{name} must be a non-empty string")

    def encode(self) -> bytes:
        parts = [self.org_id.bytes, *(v.encode("utf-8") for v in (self.table, self.column))]
        parts.append(self.row_id.encode("utf-8"))
        # Length-prefixed, so no two different contexts encode to the same bytes.
        return _AAD_TAG + b"".join(len(p).to_bytes(4, "big") + p for p in parts)

    @classmethod
    def for_row(cls, org_id: uuid.UUID, table: str, column: str, row_id: object) -> Aad:
        return cls(org_id=org_id, table=table, column=column, row_id=str(row_id))


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    try:
        return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (binascii.Error, ValueError):
        raise DecryptionError("ciphertext is malformed") from None


def _wrap_aad(version: str, aad: Aad) -> bytes:
    v = version.encode("ascii")
    return _WRAP_TAG + len(v).to_bytes(4, "big") + v + aad.encode()


@dataclass(frozen=True, slots=True)
class _Parts:
    kek_version: str
    wrapped: bytes
    nonce: bytes
    ciphertext: bytes


def _parse(token: str) -> _Parts:
    if not isinstance(token, str) or len(token) > _MAX_TOKEN_CHARS:
        raise DecryptionError("ciphertext is malformed")
    pieces = token.split(":")
    if len(pieces) != 5 or pieces[0] != TOKEN_VERSION or not VERSION_RE.fullmatch(pieces[1]):
        raise DecryptionError("ciphertext is malformed")
    wrapped, nonce, ciphertext = (_unb64(p) for p in pieces[2:])
    if len(nonce) != NONCE_BYTES or len(wrapped) != NONCE_BYTES + DEK_BYTES + 16:
        raise DecryptionError("ciphertext is malformed")
    return _Parts(pieces[1], wrapped, nonce, ciphertext)


def _wrap(dek: bytes, aad: Aad, keyring: KeyRing) -> str:
    nonce = os.urandom(NONCE_BYTES)
    sealed = AESGCM(keyring.current_kek).encrypt(nonce, dek, _wrap_aad(keyring.current, aad))
    return _b64(nonce + sealed)


def _unwrap(parts: _Parts, aad: Aad, keyring: KeyRing) -> bytes:
    try:
        kek = keyring.kek(parts.kek_version)
    except UnknownKeyVersionError:
        raise DecryptionError("ciphertext cannot be decrypted") from None
    nonce, sealed = parts.wrapped[:NONCE_BYTES], parts.wrapped[NONCE_BYTES:]
    try:
        return AESGCM(kek).decrypt(nonce, sealed, _wrap_aad(parts.kek_version, aad))
    except InvalidTag:
        raise DecryptionError("ciphertext cannot be decrypted") from None


def encrypt(plaintext: bytes, aad: Aad, keyring: KeyRing | None = None) -> str:
    """Encrypt ``plaintext`` for ``aad`` under a new data key wrapped with the current KEK."""
    ring = keyring or get_keyring()
    dek = AESGCM.generate_key(bit_length=256)
    nonce = os.urandom(NONCE_BYTES)
    ciphertext = AESGCM(dek).encrypt(nonce, plaintext, aad.encode())
    wrapped = _wrap(dek, aad, ring)
    return ":".join((TOKEN_VERSION, ring.current, wrapped, _b64(nonce), _b64(ciphertext)))


def decrypt(token: str, aad: Aad, keyring: KeyRing | None = None) -> bytes:
    """Decrypt a token made by ``encrypt`` for the same ``aad``. Raises ``DecryptionError``."""
    ring = keyring or get_keyring()
    parts = _parse(token)
    dek = _unwrap(parts, aad, ring)
    try:
        return AESGCM(dek).decrypt(parts.nonce, parts.ciphertext, aad.encode())
    except InvalidTag:
        raise DecryptionError("ciphertext cannot be decrypted") from None


def rewrap(token: str, aad: Aad, keyring: KeyRing | None = None) -> str:
    """Re-wrap the token's data key with the current KEK. The value ciphertext is kept.

    The value is authenticated first, so a tampered token is never re-wrapped.
    """
    ring = keyring or get_keyring()
    parts = _parse(token)
    dek = _unwrap(parts, aad, ring)
    try:
        AESGCM(dek).decrypt(parts.nonce, parts.ciphertext, aad.encode())
    except InvalidTag:
        raise DecryptionError("ciphertext cannot be decrypted") from None
    if parts.kek_version == ring.current:
        return token
    wrapped = _wrap(dek, aad, ring)
    return ":".join(
        (TOKEN_VERSION, ring.current, wrapped, _b64(parts.nonce), _b64(parts.ciphertext))
    )


def kek_version(token: str) -> str:
    """The KEK version a token's data key is wrapped with."""
    return _parse(token).kek_version


def current_prefix(keyring: KeyRing | None = None) -> str:
    """Token prefix of values wrapped with the current KEK (``v1:<version>:``)."""
    ring = keyring or get_keyring()
    return f"{TOKEN_VERSION}:{ring.current}:"
