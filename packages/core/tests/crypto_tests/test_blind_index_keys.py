# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Versioned blind index keys: loading, writes under the current key, lookups under
every key, and the declaration of blind index columns."""

from __future__ import annotations

import base64
import secrets
import uuid

import pytest
from sqlalchemy import Column, MetaData, String, Table, Uuid

from pairly_core.crypto.blind_index import (
    blind_index,
    blind_index_all,
    blind_index_candidates,
    blind_index_columns,
    blind_index_info,
)
from pairly_core.crypto.keys import (
    CryptoConfigError,
    KeyRing,
    UnknownKeyVersionError,
    load_keyring,
)
from pairly_core.crypto.types import EncryptedString

ORG = uuid.UUID("00000000-0000-4000-8000-0000000000b1")


def key() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()


def env(**extra: str) -> dict[str, str]:
    return {
        "PAIRLY_KEK_1": key(),
        "PAIRLY_KEK_CURRENT": "1",
        "PAIRLY_BLIND_INDEX_KEY": key(),
        **extra,
    }


def test_single_key_is_version_one() -> None:
    ring = load_keyring(env())
    assert ring.blind_index_current == "1"
    assert ring.blind_index_versions == ("1",)
    assert ring.blind_key("1") == ring.blind_index_key
    with pytest.raises(UnknownKeyVersionError, match="blind index key version"):
        ring.blind_key("2")


def test_rotation_keys_and_current_version() -> None:
    values = env(PAIRLY_BLIND_INDEX_KEY_2=key(), PAIRLY_BLIND_INDEX_KEY_CURRENT="2")
    ring = load_keyring(values)
    assert ring.blind_index_current == "2"
    assert ring.blind_index_versions == ("2", "1")
    assert ring.blind_index_key == ring.blind_key("2")
    assert values["PAIRLY_BLIND_INDEX_KEY_2"] not in repr(ring)
    staged = load_keyring(env(PAIRLY_BLIND_INDEX_KEY_2=key()))
    assert staged.blind_index_current == "1"
    assert staged.blind_index_versions == ("1", "2")
    empty_slot = load_keyring(env(PAIRLY_BLIND_INDEX_KEY_2="", PAIRLY_BLIND_INDEX_KEY_CURRENT=""))
    assert empty_slot.blind_index_versions == ("1",)
    only_new = load_keyring(
        {
            "PAIRLY_KEK_1": key(),
            "PAIRLY_KEK_CURRENT": "1",
            "PAIRLY_BLIND_INDEX_KEY_2": key(),
            "PAIRLY_BLIND_INDEX_KEY_CURRENT": "2",
        }
    )
    assert only_new.blind_index_versions == ("2",)


@pytest.mark.parametrize(
    ("values", "message"),
    [
        (env(PAIRLY_BLIND_INDEX_KEY_CURRENT="2"), "no PAIRLY_BLIND_INDEX_KEY_2 is set"),
        (env(PAIRLY_BLIND_INDEX_KEY_CURRENT="bad-version"), "1 to 16 letters or digits"),
        (env(**{"PAIRLY_BLIND_INDEX_KEY_bad-v": key()}), "1 to 16 letters or digits"),
        (env(PAIRLY_BLIND_INDEX_KEY_1=key()), "already set by PAIRLY_BLIND_INDEX_KEY"),
        (env(PAIRLY_BLIND_INDEX_KEY_2="@@"), "PAIRLY_BLIND_INDEX_KEY_2: not valid base64"),
        (
            env(PAIRLY_BLIND_INDEX_KEY_2=base64.b64encode(b"short").decode()),
            "PAIRLY_BLIND_INDEX_KEY_2: must decode to at least 32 bytes",
        ),
        (
            env(PAIRLY_BLIND_INDEX_KEY_2=base64.b64encode(bytes(32)).decode()),
            "PAIRLY_BLIND_INDEX_KEY_2: looks like a placeholder",
        ),
        ({"PAIRLY_KEK_1": key(), "PAIRLY_KEK_CURRENT": "1"}, "PAIRLY_BLIND_INDEX_KEY: missing"),
    ],
)
def test_bad_rotation_keys_fail_closed(values: dict[str, str], message: str) -> None:
    with pytest.raises(CryptoConfigError, match=message) as info:
        load_keyring(values)
    for value in values.values():
        if len(value) > 8:
            assert value not in str(info.value)


def test_keys_must_be_distinct() -> None:
    same = key()
    with pytest.raises(CryptoConfigError, match="share the same key"):
        load_keyring(env(PAIRLY_BLIND_INDEX_KEY=same, PAIRLY_BLIND_INDEX_KEY_2=same))
    kek = key()
    with pytest.raises(CryptoConfigError, match="must differ from every KEK"):
        load_keyring(env(PAIRLY_KEK_1=kek, PAIRLY_BLIND_INDEX_KEY_2=kek))


def test_key_ring_constructor_checks_the_current_key() -> None:
    one, two = secrets.token_bytes(32), secrets.token_bytes(32)
    kek = {"1": secrets.token_bytes(32)}
    ring = KeyRing(
        keks=kek,
        current="1",
        blind_index_key=one,
        blind_index_keys={"1": one, "2": two},
        blind_index_current="1",
    )
    assert ring.blind_index_versions == ("1", "2")
    with pytest.raises(ValueError, match="current blind index key"):
        KeyRing(
            keks=kek,
            current="1",
            blind_index_key=two,
            blind_index_keys={"1": one, "2": two},
            blind_index_current="1",
        )


def test_writes_use_the_current_key_and_lookups_try_every_key() -> None:
    v1 = load_keyring(env())
    raw2 = key()
    both = load_keyring(
        {
            "PAIRLY_KEK_1": base64.urlsafe_b64encode(v1.keks["1"]).decode(),
            "PAIRLY_KEK_CURRENT": "1",
            "PAIRLY_BLIND_INDEX_KEY": base64.urlsafe_b64encode(v1.blind_index_key).decode(),
            "PAIRLY_BLIND_INDEX_KEY_2": raw2,
            "PAIRLY_BLIND_INDEX_KEY_CURRENT": "2",
        }
    )
    old = blind_index("email", "Vendor@Example.test", ORG, v1)
    new = blind_index("email", "vendor@example.test", ORG, both)
    assert old is not None
    assert new is not None
    assert old != new
    assert blind_index("email", "vendor@example.test", ORG, both, version="1") == old
    assert blind_index_candidates("email", " VENDOR@example.test", ORG, both) == (new, old)
    assert blind_index_all("email", "vendor@example.test", ORG, both) == {"2": new, "1": old}
    assert blind_index_candidates("email", "not an email", ORG, both) == ()
    assert blind_index_all("email", None, ORG, both) == {}
    with pytest.raises(UnknownKeyVersionError):
        blind_index("email", "vendor@example.test", ORG, both, version="9")


def test_blind_index_columns_are_declared_by_info() -> None:
    metadata = MetaData()
    Table(
        "people",
        metadata,
        Column("id", Uuid, primary_key=True),
        Column("org_id", Uuid),
        Column("email", EncryptedString()),
        Column("email_bidx", String(64), info=blind_index_info("email", "email")),
        Column("plain", String(64)),
    )
    (found,) = blind_index_columns(metadata)
    assert (found.table.name, found.column, found.source, found.kind) == (
        "people",
        "email_bidx",
        "email",
        "email",
    )
    with pytest.raises(ValueError, match="unknown blind index kind"):
        blind_index_info("ssn", "x")  # type: ignore[arg-type]
    bad = MetaData()
    Table(
        "things",
        bad,
        Column("id", Uuid, primary_key=True),
        Column("org_id", Uuid),
        Column("name", String(10)),
        Column("name_bidx", String(64), info=blind_index_info("text", "name")),
    )
    with pytest.raises(ValueError, match="is not encrypted"):
        list(blind_index_columns(bad))
    no_org = MetaData()
    Table(
        "loose",
        no_org,
        Column("id", Uuid, primary_key=True),
        Column("email", EncryptedString()),
        Column("email_bidx", String(64), info=blind_index_info("email", "email")),
    )
    with pytest.raises(ValueError, match="no id or org_id"):
        list(blind_index_columns(no_org))


def test_core_models_declare_their_blind_indexes() -> None:
    import pairly_core.models  # noqa: F401
    from pairly_core.db import Base

    found = {(c.table.name, c.column, c.source) for c in blind_index_columns(Base.metadata)}
    assert ("inbound_emails", "sender_address_bidx", "sender_address") in found
    assert ("notifications", "recipient_bidx", "recipient_address") in found
