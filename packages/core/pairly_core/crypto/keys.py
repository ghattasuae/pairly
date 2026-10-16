# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Key encryption keys (KEKs) and the blind index key, read from the environment.

    PAIRLY_KEK_<version>     base64 (standard or URL safe), exactly 32 bytes. Several
                             versions may be set at once during a rotation.
    PAIRLY_KEK_CURRENT       the version new data keys are wrapped with.
    PAIRLY_BLIND_INDEX_KEY   base64, at least 32 bytes, HMAC key for blind indexes
                             (blind index key version ``1``).
    PAIRLY_BLIND_INDEX_KEY_<version>
                             further blind index keys, set during a rotation.
    PAIRLY_BLIND_INDEX_KEY_CURRENT
                             the blind index key version new indexes are written
                             with (default ``1``). Lookups match every configured
                             version; see ``pairly_core.crypto.blind_index``.

``<version>`` is 1 to 16 letters or digits (``1``, ``2026a``); an empty value counts
as unset. Fail closed: a missing
or malformed key raises ``CryptoConfigError`` naming every problem (never a key value),
and the API and worker refuse to start. There are no default keys.
"""

from __future__ import annotations

import base64
import binascii
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field

from pairly_core.config import ConfigError

KEK_PREFIX = "PAIRLY_KEK_"
CURRENT_ENV = "PAIRLY_KEK_CURRENT"
BLIND_INDEX_ENV = "PAIRLY_BLIND_INDEX_KEY"
BLIND_INDEX_PREFIX = "PAIRLY_BLIND_INDEX_KEY_"
BLIND_INDEX_CURRENT_ENV = "PAIRLY_BLIND_INDEX_KEY_CURRENT"
LEGACY_BLIND_VERSION = "1"
KEY_BYTES = 32
VERSION_RE = re.compile(r"^[A-Za-z0-9]{1,16}$")


class CryptoConfigError(ConfigError):
    """Keys are missing or malformed. The message lists every problem, never a key."""


def decode_key(raw: str) -> bytes:
    """Strict base64 (standard or URL safe, padding optional). Raises ``ValueError``."""
    text = raw.strip()
    if not text or not re.fullmatch(r"[A-Za-z0-9+/_-]+={0,2}", text):
        raise ValueError("not base64")
    text = text.replace("-", "+").replace("_", "/")
    text += "=" * (-len(text) % 4)
    try:
        return base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError):
        raise ValueError("not base64") from None


def _weak(key: bytes) -> bool:
    # A key made of one repeated byte (all zeros, for example) is a placeholder.
    return len(set(key)) < 8


@dataclass(frozen=True, slots=True)
class KeyRing:
    """Every configured KEK by version, the current version and the blind index keys.

    ``blind_index_key`` is the key new blind indexes are written with (version
    ``blind_index_current``); ``blind_index_keys`` holds every configured version, all
    of which lookups try. Built with only ``blind_index_key``, the ring has that single
    key as version ``1``.
    """

    keks: Mapping[str, bytes] = field(repr=False)
    current: str
    blind_index_key: bytes = field(repr=False)
    blind_index_keys: Mapping[str, bytes] = field(default_factory=dict, repr=False)
    blind_index_current: str = LEGACY_BLIND_VERSION

    def __post_init__(self) -> None:
        if not self.blind_index_keys:
            keys = {self.blind_index_current: self.blind_index_key}
            object.__setattr__(self, "blind_index_keys", keys)
        elif self.blind_index_keys.get(self.blind_index_current) != self.blind_index_key:
            raise ValueError("blind_index_key must be the current blind index key")

    @property
    def blind_index_versions(self) -> tuple[str, ...]:
        """Every blind index key version, the current one first."""
        others = sorted(v for v in self.blind_index_keys if v != self.blind_index_current)
        return (self.blind_index_current, *others)

    def blind_key(self, version: str) -> bytes:
        try:
            return self.blind_index_keys[version]
        except KeyError:
            raise UnknownKeyVersionError(version, kind="blind index key") from None

    def kek(self, version: str) -> bytes:
        try:
            return self.keks[version]
        except KeyError:
            raise UnknownKeyVersionError(version) from None

    @property
    def current_kek(self) -> bytes:
        return self.kek(self.current)

    @property
    def versions(self) -> tuple[str, ...]:
        return tuple(sorted(self.keks))


class UnknownKeyVersionError(LookupError):
    """A ciphertext names a KEK (or blind index key) version that is not configured."""

    def __init__(self, version: str, *, kind: str = "KEK") -> None:
        super().__init__(f"{kind} version {version!r} is not configured")
        self.version = version


def load_keyring(environ: Mapping[str, str] | None = None) -> KeyRing:
    """Build a ``KeyRing`` from ``environ`` (default ``os.environ``) or raise."""
    env = os.environ if environ is None else environ
    problems: list[str] = []
    keks: dict[str, bytes] = {}
    for name, value in env.items():
        if not name.startswith(KEK_PREFIX) or name == CURRENT_ENV:
            continue
        version = name[len(KEK_PREFIX) :]
        if not value.strip():
            continue  # an empty slot (for example PAIRLY_KEK_2 in compose) is unset
        if not VERSION_RE.fullmatch(version):
            problems.append(f"{name}: the version must be 1 to 16 letters or digits")
            continue
        try:
            key = decode_key(value)
        except ValueError:
            problems.append(f"{name}: not valid base64")
            continue
        if len(key) != KEY_BYTES:
            problems.append(f"{name}: must decode to exactly {KEY_BYTES} bytes")
        elif _weak(key):
            problems.append(f"{name}: looks like a placeholder, generate a random key")
        else:
            keks[version] = key

    current = env.get(CURRENT_ENV, "").strip()
    if not current:
        problems.append(f"{CURRENT_ENV}: missing (the KEK version to encrypt with)")
    elif current not in keks and not any(p.startswith(f"{KEK_PREFIX}{current}:") for p in problems):
        problems.append(f"{CURRENT_ENV}: no {KEK_PREFIX}{current} is set")
    kek_problems = [
        p for p in problems if p.startswith(KEK_PREFIX) and not p.startswith(CURRENT_ENV)
    ]
    if not keks and not kek_problems:
        problems.append(f"{KEK_PREFIX}<version>: no key encryption key is set")
    if len(set(keks.values())) != len(keks):
        problems.append(f"{KEK_PREFIX}<version>: two versions share the same key")

    blind_keys = _blind_index_keys(env, problems, keks)
    blind_current = env.get(BLIND_INDEX_CURRENT_ENV, "").strip() or LEGACY_BLIND_VERSION
    if not VERSION_RE.fullmatch(blind_current):
        problems.append(f"{BLIND_INDEX_CURRENT_ENV}: the version must be 1 to 16 letters or digits")
    elif blind_keys and blind_current not in blind_keys:
        name = (
            BLIND_INDEX_ENV
            if blind_current == LEGACY_BLIND_VERSION
            else (f"{BLIND_INDEX_PREFIX}{blind_current}")
        )
        if not any(p.startswith(f"{name}:") for p in problems):
            problems.append(f"{BLIND_INDEX_CURRENT_ENV}: no {name} is set")
    if problems:
        raise CryptoConfigError("invalid encryption keys: " + "; ".join(sorted(problems)))
    return KeyRing(
        keks=dict(keks),
        current=current,
        blind_index_key=blind_keys[blind_current],
        blind_index_keys=blind_keys,
        blind_index_current=blind_current,
    )


def _blind_index_keys(
    env: Mapping[str, str], problems: list[str], keks: Mapping[str, bytes]
) -> dict[str, bytes]:
    """Every blind index key by version (``PAIRLY_BLIND_INDEX_KEY`` is version 1)."""
    raw: dict[str, tuple[str, str]] = {}
    legacy = env.get(BLIND_INDEX_ENV, "")
    if legacy.strip():
        raw[LEGACY_BLIND_VERSION] = (BLIND_INDEX_ENV, legacy)
    for name, value in env.items():
        if not name.startswith(BLIND_INDEX_PREFIX) or name == BLIND_INDEX_CURRENT_ENV:
            continue
        if not value.strip():
            continue  # an empty rotation slot is unset
        version = name[len(BLIND_INDEX_PREFIX) :]
        if not VERSION_RE.fullmatch(version):
            problems.append(f"{name}: the version must be 1 to 16 letters or digits")
            continue
        if version in raw:
            problems.append(f"{name}: version {version} is already set by {raw[version][0]}")
            continue
        raw[version] = (name, value)
    if not raw:
        problems.append(f"{BLIND_INDEX_ENV}: missing")
        return {}
    keys: dict[str, bytes] = {}
    for version, (name, value) in sorted(raw.items()):
        try:
            key = decode_key(value)
        except ValueError:
            problems.append(f"{name}: not valid base64")
            continue
        if len(key) < KEY_BYTES:
            problems.append(f"{name}: must decode to at least {KEY_BYTES} bytes")
        elif _weak(key):
            problems.append(f"{name}: looks like a placeholder")
        elif key in keks.values():
            problems.append(f"{name}: must differ from every KEK")
        elif key in keys.values():
            problems.append(f"{name}: two blind index versions share the same key")
        else:
            keys[version] = key
    return keys


_keyring: KeyRing | None = None


def install_keyring(keyring: KeyRing | None) -> None:
    """Set (or clear, with ``None``) the process-wide key ring."""
    global _keyring
    _keyring = keyring


def get_keyring() -> KeyRing:
    """The installed key ring, loaded from the environment on first use."""
    global _keyring
    if _keyring is None:
        _keyring = load_keyring()
    return _keyring


def init_crypto(environ: Mapping[str, str] | None = None) -> KeyRing:
    """Load and install the key ring. Services call this at startup (fail closed)."""
    keyring = load_keyring(environ)
    install_keyring(keyring)
    return keyring
