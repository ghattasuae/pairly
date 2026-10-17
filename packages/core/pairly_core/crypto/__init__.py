# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Application-level encryption (WP1.14).

* ``keys``         KEKs and the blind index key from the environment (fail closed).
* ``envelope``     AES-256-GCM envelope encryption bound to (org, table, column, row).
* ``types``        ``EncryptedString`` and ``EncryptedJSON`` column types, ``seal`` and
                   ``unseal`` helpers.
* ``blind_index``  HMAC-SHA256 blind indexes with per-field normalisation.
* ``rotation``     re-wrap data keys to the current KEK (``pairly-admin crypto rotate``).
                   Not imported here, it depends on the audit log.
"""

from __future__ import annotations

from pairly_core.crypto.blind_index import blind_index
from pairly_core.crypto.envelope import Aad, DecryptionError, decrypt, encrypt, rewrap
from pairly_core.crypto.keys import (
    CryptoConfigError,
    KeyRing,
    get_keyring,
    init_crypto,
    install_keyring,
    load_keyring,
)
from pairly_core.crypto.types import (
    EncryptedJSON,
    EncryptedString,
    Plaintext,
    Sealed,
    field_aad,
    seal,
    unseal,
)

__all__ = [
    "Aad",
    "CryptoConfigError",
    "DecryptionError",
    "EncryptedJSON",
    "EncryptedString",
    "KeyRing",
    "Plaintext",
    "Sealed",
    "blind_index",
    "decrypt",
    "encrypt",
    "field_aad",
    "get_keyring",
    "init_crypto",
    "install_keyring",
    "load_keyring",
    "rewrap",
    "seal",
    "unseal",
]
