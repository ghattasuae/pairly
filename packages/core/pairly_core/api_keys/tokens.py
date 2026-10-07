# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""API key format, the server pepper and the stored hash.

Format: ``pk_<mode>_<id>_<secret>``

* ``mode``: ``live``, or ``test`` (sandbox organisations only).
* ``id``: 8 characters ``[a-z0-9]``. ``pk_<mode>_<id>`` is the visible prefix: it is
  stored in clear, shown in lists and used to find the key.
* ``secret``: 32 random bytes, URL-safe base64 (43 characters). Shown once.

Stored hash: ``HMAC-SHA256(pepper, prefix + "." + secret)``, lowercase hex.

Why a keyed HMAC and not a slow password hash (argon2id): a slow hash protects
low-entropy secrets (passwords) against offline guessing. An API key secret has 256
bits of entropy, so guessing is hopeless with any hash; a slow hash would only add tens
of milliseconds of CPU to every API request (and a denial-of-service lever). The pepper
(``PAIRLY_API_KEY_PEPPER``, from the environment, never in the database) means a stolen
database alone does not even let anyone test a guess. Binding the prefix into the MAC
stops a hash from being moved to another key row. Comparison is constant time.

Fail closed: the API refuses to start without a valid pepper (``init_api_key_pepper``).
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from pairly_core.config import ConfigError
from pairly_core.crypto.keys import decode_key

PEPPER_ENV: Final = "PAIRLY_API_KEY_PEPPER"
MIN_PEPPER_BYTES: Final = 32
ID_LENGTH: Final = 8
SECRET_BYTES: Final = 32
_ID_ALPHABET: Final = "abcdefghijklmnopqrstuvwxyz0123456789"
TOKEN_RE: Final = re.compile(r"^(pk_(live|test)_[a-z0-9]{8})_([A-Za-z0-9_-]{43})$")
PREFIX_RE: Final = re.compile(r"^pk_(live|test)_[a-z0-9]{8}$")
HASH_RE: Final = re.compile(r"^[0-9a-f]{64}$")


class KeyMode(StrEnum):
    LIVE = "live"
    TEST = "test"


class ApiKeyConfigError(ConfigError):
    """The pepper is missing or malformed. The message never contains the value."""


@dataclass(frozen=True, slots=True)
class ParsedToken:
    prefix: str
    mode: KeyMode
    secret: str


@dataclass(frozen=True, slots=True)
class NewKey:
    prefix: str
    mode: KeyMode
    secret: str  # hashed for storage; never stored or logged itself
    token: str  # shown once to the creator; never stored or logged

    def __repr__(self) -> str:
        return f"NewKey(prefix={self.prefix!r}, mode={self.mode.value!r})"


def looks_like_api_key(token: str) -> bool:
    """Cheap routing check: the bearer token is an API key, not a JWT."""
    return token.startswith("pk_")


def parse_token(token: str) -> ParsedToken | None:
    match = TOKEN_RE.fullmatch(token)
    if match is None:
        return None
    return ParsedToken(prefix=match.group(1), mode=KeyMode(match.group(2)), secret=match.group(3))


def new_key(mode: KeyMode) -> NewKey:
    key_id = "".join(secrets.choice(_ID_ALPHABET) for _ in range(ID_LENGTH))
    prefix = f"pk_{mode.value}_{key_id}"
    secret = secrets.token_urlsafe(SECRET_BYTES)
    # The secret is URL-safe base64, so it may itself contain "_": never split the token
    # on "_" to recover it (parse_token reads the fixed-length tail instead).
    return NewKey(prefix=prefix, mode=mode, secret=secret, token=f"{prefix}_{secret}")


def hash_secret(pepper: bytes, prefix: str, secret: str) -> str:
    message = f"{prefix}.{secret}".encode("ascii")
    return hmac.new(pepper, message, hashlib.sha256).hexdigest()


def verify_secret(pepper: bytes, prefix: str, secret: str, stored_hash: str) -> bool:
    return hmac.compare_digest(hash_secret(pepper, prefix, secret), stored_hash)


def load_pepper(environ: Mapping[str, str] | None = None) -> bytes:
    env = os.environ if environ is None else environ
    raw = env.get(PEPPER_ENV, "")
    if not raw.strip():
        raise ApiKeyConfigError(f"missing required setting: {PEPPER_ENV}")
    try:
        pepper = decode_key(raw)
    except ValueError:
        raise ApiKeyConfigError(f"invalid settings: {PEPPER_ENV}: not valid base64") from None
    if len(pepper) < MIN_PEPPER_BYTES:
        raise ApiKeyConfigError(
            f"invalid settings: {PEPPER_ENV}: must decode to at least {MIN_PEPPER_BYTES} bytes"
        )
    if len(set(pepper)) < 8:
        raise ApiKeyConfigError(f"invalid settings: {PEPPER_ENV}: looks like a placeholder")
    return pepper


_pepper: bytes | None = None


def install_pepper(pepper: bytes | None) -> None:
    global _pepper
    _pepper = pepper


def get_pepper() -> bytes:
    """The installed pepper, loaded from the environment on first use (fail closed)."""
    global _pepper
    if _pepper is None:
        _pepper = load_pepper()
    return _pepper


def init_api_key_pepper(environ: Mapping[str, str] | None = None) -> bytes:
    """Load and install the pepper. The API calls this at startup."""
    pepper = load_pepper(environ)
    install_pepper(pepper)
    return pepper
