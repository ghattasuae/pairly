# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Slack request signatures and the static receipt (backscatter) rules."""

from __future__ import annotations

import time
import uuid

import pytest

from pairly_core.ingestion.email import EmailIngestResult
from pairly_core.integrations.slack.service import parse_interaction
from pairly_core.integrations.slack.signature import SlackSignatureError, compute, verify
from pairly_core.notifications.receipts import read_headers, reference_for, skip_reason

SECRET = "8f742231b10e8888abcd99yyyzzz85a5"  # noqa: S105 (Slack docs example)
BODY = b"payload=%7B%22type%22%3A%22block_actions%22%7D"


def test_valid_signature() -> None:
    now = time.time()
    ts = str(int(now))
    verify(signing_secret=SECRET, timestamp=ts, signature=compute(SECRET, ts, BODY), body=BODY)


def test_known_vector() -> None:
    # Slack's documented example request.
    body = (
        b"token=xyzz0WbapA4vBCDEFasx0q6G&team_id=T1DC2JH3J&team_domain=testteamnow&"
        b"channel_id=G8PSS9T3V&channel_name=foobar&user_id=U2CERLKJA&user_name=roadrunner&"
        b"command=%2Fwebhook-collect&text=&response_url=https%3A%2F%2Fhooks.slack.com%2F"
        b"commands%2FT1DC2JH3J%2F397700885554%2F96rGlfmibIGlgcZRskXaIFfN&"
        b"trigger_id=398738663015.47445629121.803a0bc887a14d10d2c447fce8b6703c"
    )
    expected = "v0=a2114d57b48eac39b9ad189dd8316235a7b4a8d21a10bd27519666489c69b503"
    assert compute(SECRET, "1531420618", body) == expected
    verify(
        signing_secret=SECRET,
        timestamp="1531420618",
        signature=expected,
        body=body,
        now=1531420618 + 10,
    )


@pytest.mark.parametrize(
    ("ts_offset", "mutate", "reason"),
    [
        (-301, None, "stale_timestamp"),
        (+400, None, "stale_timestamp"),
        (0, "body", "signature_mismatch"),
        (0, "signature", "signature_mismatch"),
        (0, "prefix", "bad_signature"),
        (0, "missing_signature", "bad_signature"),
        (0, "missing_timestamp", "bad_timestamp"),
        (0, "timestamp_text", "bad_timestamp"),
    ],
)
def test_rejected_signatures(ts_offset: int, mutate: str | None, reason: str) -> None:
    now = 1_800_000_000.0
    ts = str(int(now) + ts_offset)
    signature: str | None = compute(SECRET, ts, BODY)
    body = BODY
    timestamp: str | None = ts
    if mutate == "body":
        body = BODY + b"x"
    elif mutate == "signature":
        assert signature is not None
        signature = signature[:-1] + ("0" if signature[-1] != "0" else "1")
    elif mutate == "prefix":
        assert signature is not None
        signature = "v1=" + signature[3:]
    elif mutate == "missing_signature":
        signature = None
    elif mutate == "missing_timestamp":
        timestamp = None
    elif mutate == "timestamp_text":
        timestamp = "12ab"
    with pytest.raises(SlackSignatureError) as exc:
        verify(signing_secret=SECRET, timestamp=timestamp, signature=signature, body=body, now=now)
    assert exc.value.reason == reason


def test_signature_needs_a_secret_and_a_small_body() -> None:
    with pytest.raises(SlackSignatureError):
        verify(signing_secret="", timestamp="1", signature="v0=x", body=b"", now=1)
    big = b"x" * (256 * 1024 + 1)
    with pytest.raises(SlackSignatureError):
        verify(
            signing_secret=SECRET,
            timestamp="1",
            signature=compute(SECRET, "1", big),
            body=big,
            now=1,
        )


def test_parse_interaction() -> None:
    payload = {
        "type": "block_actions",
        "team": {"id": "T0001"},
        "user": {"id": "U0001"},
        "response_url": "https://hooks.slack.com/actions/T0001/1/x",
        "actions": [{"action_id": "approve", "action_ts": "1700000000.123456", "value": "v"}],
    }
    parsed = parse_interaction(payload)
    assert parsed is not None
    assert (parsed.team_id, parsed.slack_user_id, parsed.action_id) == ("T0001", "U0001", "approve")
    for bad in (
        {**payload, "type": "view_submission"},
        {**payload, "team": {"id": "evil"}},
        {**payload, "actions": [{"action_id": "approve", "action_ts": "x", "value": "v"}]},
        {**payload, "actions": []},
    ):
        assert parse_interaction(bad) is None


# ------------------------------------------------------------------ receipts


def _raw(*headers: str, sender: str = "Billing <billing@vendor.example>") -> bytes:
    lines = [f"From: {sender}", "To: pay+acme.tok@inbound.pairly.localhost", "Subject: Invoice"]
    lines += ["Message-ID: <m1@vendor.example>", *headers, "", "Body"]
    return "\r\n".join(lines).encode()


ROUTED = EmailIngestResult(
    "ingested", org_id=uuid.uuid4(), workflow="pay", inbound_email_id=uuid.uuid4()
)
OWN = ["inbound.pairly.localhost", "pairly.localhost"]


@pytest.mark.parametrize(
    ("raw", "result", "reason"),
    [
        (_raw(), ROUTED, None),
        (_raw(), EmailIngestResult("unroutable"), "status_unroutable"),
        (_raw(), EmailIngestResult("too_large"), "status_too_large"),
        (
            _raw(),
            EmailIngestResult("duplicate", org_id=ROUTED.org_id, inbound_email_id=uuid.uuid4()),
            "status_duplicate",
        ),
        (_raw("Auto-Submitted: auto-replied"), ROUTED, "auto_submitted"),
        (_raw("Auto-Submitted: no"), ROUTED, "auto_submitted"),
        (_raw("Precedence: bulk"), ROUTED, "bulk"),
        (_raw("List-Id: <news.vendor.example>"), ROUTED, "bulk"),
        (_raw("List-Unsubscribe: <mailto:u@vendor.example>"), ROUTED, "bulk"),
        (_raw("X-Auto-Response-Suppress: All"), ROUTED, "autoreply_suppressed"),
        (_raw("Return-Path: <>"), ROUTED, "null_return_path"),
        (_raw(sender="MAILER-DAEMON@vendor.example"), ROUTED, "role_address"),
        (_raw(sender="no-reply@vendor.example"), ROUTED, "role_address"),
        (_raw(sender="list-bounces@vendor.example"), ROUTED, "role_address"),
        (_raw(sender="owner-ap@vendor.example"), ROUTED, "role_address"),
        (_raw(sender="robot@pairly.localhost"), ROUTED, "own_domain"),
        (_raw(sender="x@mail.inbound.pairly.localhost"), ROUTED, "own_domain"),
        (_raw(sender="undisclosed"), ROUTED, "no_sender"),
    ],
)
def test_receipt_rules(raw: bytes, result: EmailIngestResult, reason: str | None) -> None:
    assert skip_reason(read_headers(raw), result, OWN) == reason


def test_reply_to_is_never_used() -> None:
    headers = read_headers(_raw("Reply-To: victim@third-party.example"))
    assert headers.sender == "billing@vendor.example"


def test_reference_is_stable() -> None:
    email_id = uuid.UUID("3f2a9c1b-0d00-4000-8000-000000000000")
    assert reference_for(email_id) == "REF-3F2A9C1B0D"
