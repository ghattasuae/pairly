# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""A workflow approval that binds one message (``allow_bound_message``): it covers only
the registered template, only the exact message hash its intact payload names."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from pairly_core.notifications.service import (
    BOUND_MESSAGE_KEY,
    _binds,
    allow_bound_message,
    bound_message_types,
    message_hash,
)

TEMPLATE = "test.bound.offer"
OTHER = "test.bound.rejection"
DIGEST = "a" * 64


def _approval(payload: dict[str, Any], approval_type: str = "test.offer", **kw: Any) -> Any:
    return SimpleNamespace(
        approval_type=approval_type,
        payload=payload,
        payload_hash=kw.get("payload_hash", message_hash(payload)),
    )


def test_classic_message_approval_still_binds_by_its_own_hash() -> None:
    approval = SimpleNamespace(approval_type="x.message", payload={}, payload_hash=DIGEST)
    assert _binds(approval, OTHER, DIGEST)  # type: ignore[arg-type]
    assert not _binds(approval, OTHER, "b" * 64)  # type: ignore[arg-type]


def test_registered_type_binds_the_named_message_only() -> None:
    allow_bound_message(TEMPLATE, "test.offer")
    allow_bound_message(TEMPLATE, "test.offer")  # idempotent
    assert bound_message_types(TEMPLATE) == frozenset({"test.offer"})
    approval = _approval({"offer_id": "1", BOUND_MESSAGE_KEY: DIGEST})
    assert _binds(approval, TEMPLATE, DIGEST)
    # Another rendered email (any change) does not match.
    assert not _binds(approval, TEMPLATE, "b" * 64)
    # Another template is never covered by the offer approval.
    assert not _binds(approval, OTHER, DIGEST)
    # Another approval type is never covered either.
    assert not _binds(_approval({BOUND_MESSAGE_KEY: DIGEST}, "test.other"), TEMPLATE, DIGEST)


def test_a_tampered_stored_payload_does_not_bind() -> None:
    allow_bound_message(TEMPLATE, "test.offer")
    original = {"offer_id": "1", BOUND_MESSAGE_KEY: "c" * 64}
    # The stored payload was changed to name another message: its hash no longer matches.
    tampered = _approval(
        {"offer_id": "1", BOUND_MESSAGE_KEY: DIGEST}, payload_hash=message_hash(original)
    )
    assert not _binds(tampered, TEMPLATE, DIGEST)
    assert not _binds(_approval({"offer_id": "1"}), TEMPLATE, DIGEST)
