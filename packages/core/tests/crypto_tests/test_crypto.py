# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Envelope encryption, key loading, column types and blind indexes (no database)."""

from __future__ import annotations

import base64
import secrets
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy.dialects import postgresql

from pairly_core.crypto import keys as keys_mod
from pairly_core.crypto.blind_index import (
    blind_index,
    normalize_email,
    normalize_iban,
    normalize_phone,
    normalize_text,
)
from pairly_core.crypto.envelope import (
    Aad,
    DecryptionError,
    current_prefix,
    decrypt,
    encrypt,
    kek_version,
    rewrap,
)
from pairly_core.crypto.keys import (
    CryptoConfigError,
    KeyRing,
    UnknownKeyVersionError,
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
    encrypted_columns,
    field_aad,
    seal,
    unseal,
)
from pairly_core.db import Base
from pairly_core.ingestion.models import InboundEmail
from pairly_core.models.llm import ModelCall

ORG_A = uuid.UUID("00000000-0000-4000-8000-00000000000a")
ORG_B = uuid.UUID("00000000-0000-4000-8000-00000000000b")
DIALECT = postgresql.dialect()  # type: ignore[no-untyped-call]


def key() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()


def env(**extra: str) -> dict[str, str]:
    base = {
        "PAIRLY_KEK_1": key(),
        "PAIRLY_KEK_CURRENT": "1",
        "PAIRLY_BLIND_INDEX_KEY": key(),
    }
    base.update(extra)
    return base


@pytest.fixture
def ring() -> KeyRing:
    return load_keyring(env())


@pytest.fixture
def installed(ring: KeyRing) -> Iterator[KeyRing]:
    previous = keys_mod._keyring
    install_keyring(ring)
    yield ring
    install_keyring(previous)


def aad(row: str = "r1", org: uuid.UUID = ORG_A, column: str = "request") -> Aad:
    return Aad.for_row(org, "model_calls", column, row)


# ------------------------------------------------------------------ keys


def test_load_keyring_reads_every_version_and_the_current_one() -> None:
    values = env(PAIRLY_KEK_2=key(), PAIRLY_KEK_CURRENT="2")
    ring = load_keyring(values)
    assert ring.current == "2"
    assert ring.versions == ("1", "2")
    assert len(ring.current_kek) == 32
    assert "PAIRLY" not in repr(ring)
    assert values["PAIRLY_KEK_1"] not in repr(ring)
    with pytest.raises(UnknownKeyVersionError):
        ring.kek("9")


@pytest.mark.parametrize(
    ("values", "message"),
    [
        ({"PAIRLY_KEK_CURRENT": "1", "PAIRLY_BLIND_INDEX_KEY": key()}, "no key encryption key"),
        ({"PAIRLY_KEK_1": key(), "PAIRLY_BLIND_INDEX_KEY": key()}, "PAIRLY_KEK_CURRENT: missing"),
        (env(PAIRLY_KEK_CURRENT="7"), "no PAIRLY_KEK_7 is set"),
        (env(PAIRLY_KEK_1="not base64!!"), "PAIRLY_KEK_1: not valid base64"),
        (
            env(PAIRLY_KEK_1=base64.b64encode(b"short").decode()),
            "PAIRLY_KEK_1: must decode to exactly 32 bytes",
        ),
        (env(PAIRLY_KEK_1=base64.b64encode(bytes(32)).decode()), "placeholder"),
        ({"PAIRLY_KEK_1": key(), "PAIRLY_KEK_CURRENT": "1"}, "PAIRLY_BLIND_INDEX_KEY: missing"),
        (env(PAIRLY_BLIND_INDEX_KEY="@@"), "PAIRLY_BLIND_INDEX_KEY: not valid base64"),
        (
            env(PAIRLY_BLIND_INDEX_KEY=base64.b64encode(b"x" * 8).decode()),
            "at least 32 bytes",
        ),
        (env(**{"PAIRLY_KEK_bad-version": key()}), "1 to 16 letters or digits"),
    ],
)
def test_bad_or_missing_keys_fail_and_name_the_problem(
    values: dict[str, str], message: str
) -> None:
    with pytest.raises(CryptoConfigError, match=message) as info:
        load_keyring(values)
    for value in values.values():
        if len(value) > 8:
            assert value not in str(info.value)


def test_duplicate_or_reused_keys_are_refused() -> None:
    same = key()
    with pytest.raises(CryptoConfigError, match="share the same key"):
        load_keyring(env(PAIRLY_KEK_1=same, PAIRLY_KEK_2=same))
    with pytest.raises(CryptoConfigError, match="must differ from every KEK"):
        load_keyring(env(PAIRLY_KEK_1=same, PAIRLY_BLIND_INDEX_KEY=same))


def test_empty_slot_counts_as_unset_and_standard_base64_is_accepted() -> None:
    std = base64.b64encode(secrets.token_bytes(32)).decode()
    ring = load_keyring(env(PAIRLY_KEK_1=std, PAIRLY_KEK_2=""))
    assert ring.versions == ("1",)


def test_init_crypto_installs_and_get_keyring_loads_lazily(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    previous = keys_mod._keyring
    try:
        values = env()
        ring = init_crypto(values)
        assert get_keyring() is ring
        install_keyring(None)
        for name, value in values.items():
            monkeypatch.setenv(name, value)
        assert get_keyring().current == "1"
        install_keyring(None)
        monkeypatch.delenv("PAIRLY_KEK_CURRENT")
        with pytest.raises(CryptoConfigError):
            get_keyring()
    finally:
        install_keyring(previous)


# ------------------------------------------------------------------ envelope


def test_round_trip_and_token_format(ring: KeyRing) -> None:
    token = encrypt(b"secret value", aad(), ring)
    parts = token.split(":")
    assert parts[0] == "v1"
    assert parts[1] == "1"
    assert len(parts) == 5
    assert b"secret" not in token.encode()
    assert decrypt(token, aad(), ring) == b"secret value"
    assert kek_version(token) == "1"
    assert token.startswith(current_prefix(ring))
    # Fresh data key and nonce every time.
    assert encrypt(b"secret value", aad(), ring) != token


@pytest.mark.parametrize(
    "other",
    [
        aad(row="r2"),
        aad(column="response"),
        Aad.for_row(ORG_A, "inbound_emails", "request", "r1"),
        aad(org=ORG_B),
    ],
    ids=["other-row", "other-column", "other-table", "other-tenant"],
)
def test_ciphertext_is_bound_to_its_row_column_table_and_tenant(ring: KeyRing, other: Aad) -> None:
    token = encrypt(b"bound", aad(), ring)
    with pytest.raises(DecryptionError):
        decrypt(token, other, ring)


@pytest.mark.parametrize("part", [2, 3, 4])
def test_any_tampering_fails(ring: KeyRing, part: int) -> None:
    token = encrypt(b"tamper me", aad(), ring)
    pieces = token.split(":")
    raw = bytearray(base64.urlsafe_b64decode(pieces[part] + "=" * (-len(pieces[part]) % 4)))
    raw[-1] ^= 0x01
    pieces[part] = base64.urlsafe_b64encode(bytes(raw)).rstrip(b"=").decode()
    with pytest.raises(DecryptionError):
        decrypt(":".join(pieces), aad(), ring)


def test_wrong_kek_unknown_version_and_garbage_fail(ring: KeyRing) -> None:
    token = encrypt(b"x", aad(), ring)
    other = load_keyring(env())
    with pytest.raises(DecryptionError):
        decrypt(token, aad(), other)
    relabelled = token.replace("v1:1:", "v1:2:", 1)
    two = load_keyring(env(PAIRLY_KEK_2=key()))
    with pytest.raises(DecryptionError):
        decrypt(relabelled, aad(), two)
    for bad in ("", "v2:1:a:b:c", "v1:1:!!:b:c", "v1:1:" + "A" * 80 + ":AAAA:AAAA", "plain"):
        with pytest.raises(DecryptionError):
            decrypt(bad, aad(), ring)


def test_rewrap_moves_to_the_current_kek_and_keeps_the_value(ring: KeyRing) -> None:
    old = encrypt(b"rotate me", aad(), ring)
    both = KeyRing(
        keks={**ring.keks, "2": secrets.token_bytes(32)},
        current="2",
        blind_index_key=ring.blind_index_key,
    )
    new = rewrap(old, aad(), both)
    assert kek_version(new) == "2"
    assert new.split(":")[3:] == old.split(":")[3:]  # same nonce and value ciphertext
    assert decrypt(new, aad(), both) == b"rotate me"
    assert rewrap(new, aad(), both) == new  # already current: unchanged
    only_new = KeyRing(keks={"2": both.keks["2"]}, current="2", blind_index_key=b"k" * 32)
    assert decrypt(new, aad(), only_new) == b"rotate me"
    with pytest.raises(DecryptionError):
        rewrap(old, aad(row="elsewhere"), both)


def test_aad_validates_its_fields() -> None:
    with pytest.raises(TypeError):
        Aad(org_id="not-a-uuid", table="t", column="c", row_id="r")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="column"):
        Aad(org_id=ORG_A, table="t", column="", row_id="r")
    # Length prefixes: shifting a separator between fields changes the encoding.
    assert Aad(ORG_A, "ab", "c", "r").encode() != Aad(ORG_A, "a", "bc", "r").encode()


# ------------------------------------------------------------------ column types


def test_encrypted_columns_refuse_plaintext(installed: KeyRing) -> None:
    for column_type, value in ((EncryptedJSON(), {"a": 1}), (EncryptedString(), "x@example.test")):
        with pytest.raises(TypeError, match="refusing to write plaintext"):
            column_type.process_bind_param(value, DIALECT)
        assert column_type.process_bind_param(None, DIALECT) is None
        assert column_type.process_result_value(None, DIALECT) is None


def test_seal_bind_and_unseal_round_trip(installed: KeyRing) -> None:
    row_id = uuid.uuid4()
    sealed = seal(ModelCall, "request", {"inputs": {"b": 2, "a": 1}}, org_id=ORG_A, row_id=row_id)
    assert "hidden" in repr(sealed)
    assert "inputs" not in repr(sealed)
    token = EncryptedJSON().process_bind_param(sealed, DIALECT)
    assert token is not None
    assert token.startswith("v1:1:")
    result = EncryptedJSON().process_result_value(token, DIALECT)
    assert isinstance(result, Sealed)
    assert repr(result) == "Sealed(<ciphertext>)"
    assert EncryptedJSON().process_bind_param(result, DIALECT) == token  # copied as is
    assert result.kek_version == "1"

    class Row:
        __tablename__ = "model_calls"
        __table__ = ModelCall.__table__

        def __init__(self, request: Any) -> None:
            self.org_id = ORG_A
            self.id = row_id
            self.request = request
            self.response = None

    assert unseal(Row(result), "request") == {"inputs": {"a": 1, "b": 2}}
    assert unseal(Row(result), "response") is None
    moved = Row(result)
    moved.id = uuid.uuid4()
    with pytest.raises(DecryptionError):
        unseal(moved, "request")
    with pytest.raises(TypeError):
        unseal(Row("plain"), "request")
    assert seal(ModelCall, "response", None, org_id=ORG_A, row_id=row_id).value is None
    assert EncryptedJSON().process_bind_param(Plaintext(None, aad()), DIALECT) is None


def test_string_column_and_aad_helpers(installed: KeyRing) -> None:
    row_id = uuid.uuid4()
    sealed = seal(InboundEmail, "sender_address", "a@example.test", org_id=ORG_A, row_id=row_id)
    token = EncryptedString().process_bind_param(sealed, DIALECT)
    assert token is not None
    expected = field_aad(InboundEmail, "sender_address", org_id=ORG_A, row_id=row_id)
    assert Sealed(token).open_text(expected) == "a@example.test"
    with pytest.raises(TypeError, match="str"):
        EncryptedString().process_bind_param(
            seal(InboundEmail, "sender_address", 5, org_id=ORG_A, row_id=row_id), DIALECT
        )
    with pytest.raises(ValueError, match="not an encrypted column"):
        seal(InboundEmail, "workflow", "pay", org_id=ORG_A, row_id=row_id)
    with pytest.raises(ValueError, match="no column"):
        field_aad(InboundEmail, "nope", org_id=ORG_A, row_id=row_id)
    with pytest.raises(TypeError):
        Sealed(b"bytes")  # type: ignore[arg-type]


def test_encrypted_columns_are_listed_for_rotation() -> None:
    import pairly_core.models  # noqa: F401

    found = {(c.table.name, c.column, c.kind) for c in encrypted_columns(Base.metadata)}
    assert {
        ("model_calls", "request", "json"),
        ("model_calls", "response", "json"),
        ("inbound_emails", "sender_address", "string"),
    } <= found


# ------------------------------------------------------------------ blind index


def test_normalisers() -> None:
    assert normalize_email("  Jordan.Example@EXAMPLE.test ") == "jordan.example@example.test"
    assert normalize_email("mailto:a@b.test") == "a@b.test"
    assert normalize_email("x@b\u00fccher.example") == "x@xn--bcher-kva.example"
    for bad in ("no-at-sign", "a@b", "a@-bad-.test", "a b@c.test"):
        assert normalize_email(bad) is None
    for phone in ("+971 50 123 4567", "050 123 4567", "00971501234567", "+971 (0)50-123-4567"):
        assert normalize_phone(phone) == "+971501234567"
    assert normalize_phone("04 123 4567") == "+97141234567"
    assert normalize_phone("+44 20 7946 0000") == "+442079460000"
    assert normalize_phone("+971 50 123 4567 ext 12") == "+971501234567"
    for bad in ("123", "call me", "+0 12 345 6789"):
        assert normalize_phone(bad) is None
    assert normalize_iban("ae07 0331 2345 6789 0123 456") == "AE070331234567890123456"
    assert normalize_iban("AE07 0331 2345 6789 0123 457") is None  # checksum
    assert normalize_iban("GB33BUKB20201555555555") == "GB33BUKB20201555555555"
    assert normalize_text("  Example\u00a0Trading  LLC ") == "example trading llc"
    assert normalize_text("   ") is None


def test_blind_index_is_stable_normalised_and_tenant_scoped(ring: KeyRing) -> None:
    one = blind_index("email", "Jordan@Example.test", ORG_A, ring)
    assert one is not None
    assert len(one) == 64
    assert blind_index("email", " jordan@example.TEST", ORG_A, ring) == one
    assert blind_index("email", "jordan@example.test", ORG_B, ring) != one
    assert blind_index("text", "jordan@example.test", ORG_A, ring) != one
    other_key = load_keyring(env())
    assert blind_index("email", "jordan@example.test", ORG_A, other_key) != one
    assert blind_index("phone", "050 123 4567", ORG_A, ring) == blind_index(
        "phone", "+971501234567", ORG_A, ring
    )
    assert blind_index("iban", "AE07 0331 2345 6789 0123 457", ORG_A, ring) is None
    assert blind_index("email", None, ORG_A, ring) is None
    with pytest.raises(ValueError, match="unknown"):
        blind_index("ssn", "x", ORG_A, ring)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        blind_index("email", "a@b.test", "org", ring)  # type: ignore[arg-type]
