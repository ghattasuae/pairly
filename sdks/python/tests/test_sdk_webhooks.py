# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Proofly Technology
"""Webhook helpers: they verify what the engine signs, byte for byte, and refuse the rest."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from pairly import webhooks
from pairly.webhooks import (
    WebhookSignatureError,
    batch_completed_event,
    build_event,
    construct_event,
    sign_payload,
    signed_delivery,
    verify_signature,
)
from pairly_core.webhooks import events as engine_events
from pairly_core.webhooks import signature as engine

SECRET = "whsec_" + "k" * 40
NOW = 1_800_000_000


def test_the_sdk_verifies_what_the_engine_signs() -> None:
    org, event_id = uuid.uuid4(), uuid.uuid4()
    created = datetime(2026, 10, 16, 8, 0, tzinfo=UTC)
    data = {"batch_id": "b1", "workflow": "pay", "status": "completed", "counts": {"accepted": 1}}
    envelope = engine_events.envelope(
        event_id=event_id,
        event_type="batch.completed",
        created_at=created,
        org_id=org,
        data=data,
    )
    body = engine_events.serialise(envelope)
    header = engine.sign_payload(SECRET, body, timestamp=NOW)
    assert verify_signature(body, header, SECRET, now=NOW) == NOW
    # The SDK builds the very same bytes and signature.
    mine = build_event(
        "batch.completed", data, org_id=org, event_id=f"evt_{event_id.hex}", created_at=created
    )
    assert webhooks.serialise(mine.to_dict()) == body
    assert sign_payload(SECRET, body, NOW) == header
    # And the engine verifies what the SDK signs.
    sent, headers = signed_delivery(mine, SECRET, timestamp=NOW)
    assert engine.verify_webhook_signature(sent, headers["Pairly-Signature"], SECRET, now=NOW)
    assert headers["Pairly-Event-Type"] == "batch.completed"


def test_tampering_replay_and_bad_headers_are_refused() -> None:
    body = b'{"id":"evt_1","type":"document.received"}'
    header = sign_payload(SECRET, body, NOW)
    bad = [
        (body + b" ", header, NOW),
        (body, header, NOW + 301),
        (body, header, NOW - 301),
        (body, header.replace("v1=", "v0="), NOW),
        (body, "", NOW),
        (body, "t=abc,v1=00", NOW),
        (body, f"t={NOW}", NOW),
        (body, f"t={NOW},t={NOW},v1=00", NOW),
        (body, "x" * 5000, NOW),
    ]
    for payload, sig, now in bad:
        with pytest.raises(WebhookSignatureError):
            verify_signature(payload, sig, SECRET, now=now)
    with pytest.raises(WebhookSignatureError):
        verify_signature(body, header, "", now=NOW)
    with pytest.raises(WebhookSignatureError):
        verify_signature(body, header, "whsec_other", now=NOW)


def test_a_rotated_secret_still_verifies_during_the_overlap() -> None:
    body = b"{}"
    old = engine.compute_signature("whsec_old", NOW, body)
    new = engine.compute_signature(SECRET, NOW, body)
    header = f"t={NOW},v1={old},v1={new}"
    assert verify_signature(body, header, SECRET, now=NOW) == NOW


def test_construct_event_gives_typed_data() -> None:
    org = uuid.uuid4()
    event = batch_completed_event(
        {
            "batch_id": "b1",
            "workflow": "pay",
            "status": "completed",
            "metadata": {"run": 1},
            "counts": {"accepted": 1},
            "items": [],
        },
        org_id=org,
    )
    body, headers = signed_delivery(event, SECRET)
    parsed = construct_event(body, headers["Pairly-Signature"], SECRET)
    assert parsed.type == "batch.completed"
    assert parsed.org_id == str(org)
    assert parsed.batch_completed()["metadata"] == {"run": 1}
    assert parsed.id.startswith("evt_")
    with pytest.raises(ValueError, match=r"not approval\.decided"):
        parsed.approval_decided()
    with pytest.raises(WebhookSignatureError):
        construct_event(body, headers["Pairly-Signature"], "whsec_wrong")


def test_every_engine_event_type_has_a_typed_helper() -> None:
    keys = {e.key for e in engine_events.CORE_EVENTS}
    helpers = {"approval.decided", "document.received", "decision.recorded", "batch.completed"}
    assert keys == helpers
