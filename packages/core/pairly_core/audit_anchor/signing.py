# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Ed25519 signatures over audit chain heads.

Keys come from the environment, never from the database, so someone who can rewrite
the database cannot forge an anchor:

* ``PAIRLY_AUDIT_SIGNING_KEY``: the private key, base64url of the 32-byte Ed25519 seed.
  Only the worker holds it (it signs the nightly anchors); the worker refuses to start
  without it.
* ``PAIRLY_AUDIT_ANCHOR_PUBLIC_KEY``: the matching public key, base64url of 32 bytes.
  The API holds this one only: it checks anchors and publishes the key at
  ``GET /v1/audit/anchor-key``. When both are set they must match.

A signed head is canonical JSON::

    {"anchored_at": "...Z", "format": "pairly.audit_anchor.v1", "hash": "<64 hex>",
     "key_id": "<16 hex>", "org_id": "<uuid>", "seq": 42, "signature": "<base64url>"}

The signature covers the same object without ``signature`` (canonical JSON, UTF-8).
``key_id`` is the first 16 hex characters of SHA-256 of the raw public key.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from pairly_core.audit import canonical_json, format_timestamp
from pairly_core.config import ConfigError

SIGNING_KEY_ENV: Final = "PAIRLY_AUDIT_SIGNING_KEY"
PUBLIC_KEY_ENV: Final = "PAIRLY_AUDIT_ANCHOR_PUBLIC_KEY"
ANCHOR_FORMAT: Final = "pairly.audit_anchor.v1"
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_KEY_ID = re.compile(r"^[0-9a-f]{16}$")


class AnchorError(ValueError):
    """An anchor is malformed or its signature does not verify."""


def _decode_key(raw: str, name: str) -> bytes:
    value = raw.strip()
    try:
        data = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (binascii.Error, ValueError):
        raise ConfigError(f"{name} must be base64url") from None
    if len(data) != 32:
        raise ConfigError(f"{name} must decode to 32 bytes")
    if len(set(data)) < 8:
        raise ConfigError(f"{name} looks like a placeholder, not a random key")
    return data


def encode_key(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii")


def public_bytes(key: Ed25519PublicKey) -> bytes:
    return key.public_bytes(Encoding.Raw, PublicFormat.Raw)


def key_id(key: Ed25519PublicKey) -> str:
    return hashlib.sha256(public_bytes(key)).hexdigest()[:16]


def load_signing_key(environ: Mapping[str, str] | None = None) -> Ed25519PrivateKey:
    """The private key from ``PAIRLY_AUDIT_SIGNING_KEY`` or ``ConfigError`` (fail closed)."""
    env = os.environ if environ is None else environ
    raw = env.get(SIGNING_KEY_ENV, "")
    if not raw.strip():
        raise ConfigError(f"missing required settings: {SIGNING_KEY_ENV}")
    key = Ed25519PrivateKey.from_private_bytes(_decode_key(raw, SIGNING_KEY_ENV))
    published = env.get(PUBLIC_KEY_ENV, "")
    if published.strip():
        expected = _decode_key(published, PUBLIC_KEY_ENV)
        if public_bytes(key.public_key()) != expected:
            raise ConfigError(f"{PUBLIC_KEY_ENV} does not match {SIGNING_KEY_ENV}")
    return key


def load_public_key(environ: Mapping[str, str] | None = None) -> Ed25519PublicKey | None:
    """The public key from ``PAIRLY_AUDIT_ANCHOR_PUBLIC_KEY``, or None when unset."""
    env = os.environ if environ is None else environ
    raw = env.get(PUBLIC_KEY_ENV, "")
    if not raw.strip():
        return None
    return Ed25519PublicKey.from_public_bytes(_decode_key(raw, PUBLIC_KEY_ENV))


@dataclass(frozen=True, slots=True)
class AnchoredHead:
    org_id: uuid.UUID
    seq: int
    hash: str
    anchored_at: str
    key_id: str

    def body(self) -> dict[str, Any]:
        return {
            "format": ANCHOR_FORMAT,
            "org_id": str(self.org_id),
            "seq": self.seq,
            "hash": self.hash,
            "anchored_at": self.anchored_at,
            "key_id": self.key_id,
        }


def sign_head(
    key: Ed25519PrivateKey, *, org_id: uuid.UUID, seq: int, head_hash: str, at: datetime
) -> bytes:
    """The signed anchor document (canonical JSON bytes)."""
    if seq < 1 or not _HEX64.fullmatch(head_hash):
        raise AnchorError("an anchor needs seq >= 1 and a 64 hex hash")
    head = AnchoredHead(org_id, seq, head_hash, format_timestamp(at), key_id(key.public_key()))
    body = head.body()
    signature = key.sign(canonical_json(body))
    return canonical_json(body | {"signature": encode_key(signature)})


def verify_head(data: bytes, keys: Mapping[str, Ed25519PublicKey]) -> AnchoredHead:
    """Parse and verify one anchor document. Raises ``AnchorError``."""
    try:
        doc = json.loads(data)
    except (ValueError, UnicodeDecodeError):
        raise AnchorError("anchor is not JSON") from None
    if not isinstance(doc, dict) or doc.get("format") != ANCHOR_FORMAT:
        raise AnchorError("not an audit anchor")
    signature = doc.pop("signature", None)
    try:
        head = AnchoredHead(
            org_id=uuid.UUID(str(doc["org_id"])),
            seq=int(doc["seq"]),
            hash=str(doc["hash"]),
            anchored_at=str(doc["anchored_at"]),
            key_id=str(doc["key_id"]),
        )
    except (KeyError, ValueError, TypeError):
        raise AnchorError("anchor fields are invalid") from None
    if head.body() != doc or not _HEX64.fullmatch(head.hash) or not _KEY_ID.fullmatch(head.key_id):
        raise AnchorError("anchor fields are invalid")
    key = keys.get(head.key_id)
    if key is None:
        raise AnchorError("anchor signed by an unknown key")
    if not isinstance(signature, str):
        raise AnchorError("anchor has no signature")
    try:
        raw = base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4))
        key.verify(raw, canonical_json(head.body()))
    except (InvalidSignature, binascii.Error, ValueError):
        raise AnchorError("anchor signature does not verify") from None
    return head
