# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Decision records and audit hardening without a database: ids, exact encoding,
right-to-left ordering, the redaction policy, receipts, anchor signatures and the
anchored chain check."""

from __future__ import annotations

import base64
import io
import json
import secrets
import unicodedata
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pdfplumber
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from pairly_core.audit import GENESIS_HASH, AuditRecord, compute_hash, verify_records
from pairly_core.audit_anchor.anchoring import public_keys
from pairly_core.audit_anchor.signing import (
    PUBLIC_KEY_ENV,
    SIGNING_KEY_ENV,
    AnchorError,
    encode_key,
    load_public_key,
    load_signing_key,
    public_bytes,
    sign_head,
    verify_head,
)
from pairly_core.audit_anchor.store import InMemoryAnchorStore, anchor_key
from pairly_core.config import ConfigError
from pairly_core.decisions.bidi import shape, visual_order
from pairly_core.decisions.ids import is_decision_id, new_decision_id
from pairly_core.decisions.models import DecisionRecord
from pairly_core.decisions.permissions import (
    APPEAL_CREATE,
    DECISION_PERSONAL_READ,
    DECISION_READ,
)
from pairly_core.decisions.receipts import (
    RECEIPT_POLICY,
    Audience,
    ReceiptContext,
    SubjectRef,
    Visibility,
    audience_for,
    receipt,
    redact,
    render_pdf,
    subject_receipt,
    visibility,
)
from pairly_core.decisions.scope import authorize_appeal_creation, create_permission
from pairly_core.decisions.serialize import (
    EncodingError,
    decode_typed,
    encode_typed,
    fingerprint,
)
from pairly_core.errors import NotFoundError
from pairly_core.rbac import Actor, ActorKind, PermissionDeniedError

ORG = uuid.UUID("00000000-0000-4000-8000-000000000001")


# ------------------------------------------------------------------ ids and encoding


def test_decision_ids_are_stable_format_and_time_ordered() -> None:
    first = new_decision_id(now_ms=1_000)
    second = new_decision_id(now_ms=2_000)
    assert is_decision_id(first)
    assert len(first) == 30
    assert first[:14] < second[:14]
    assert not is_decision_id("dec_short")
    assert not is_decision_id(first.lower())
    with pytest.raises(ValueError, match="range"):
        new_decision_id(now_ms=-1)


def test_typed_encoding_round_trips_exact_values() -> None:
    value = {
        "amount": Decimal("100000.10"),
        "when": date(2026, 10, 7),
        "at": datetime(2026, 10, 7, 8, 30, tzinfo=UTC),
        "ratio": 0.1,
        "id": uuid.UUID(int=5),
        "items": [Decimal("1.5"), "x", None, True, 3],
    }
    encoded = encode_typed(value)
    assert json.loads(json.dumps(encoded)) == encoded
    assert decode_typed(encoded) == value
    with pytest.raises(EncodingError):
        encode_typed({"__pairly_type__": "x"})
    with pytest.raises(EncodingError):
        encode_typed(object())
    assert fingerprint("a") != fingerprint("b")
    assert fingerprint({"b": 1, "a": 2}) == fingerprint({"a": 2, "b": 1})


# ------------------------------------------------------------------ bidi


@pytest.mark.parametrize(
    ("logical", "visual"),
    [
        ("abc", "abc"),
        ("مرحبا", "ابحرم"),
        ("مرحبا 123", "123 ابحرم"),
        ("القرار: dec_01", "dec_01 :رارقلا"),
        ("(مرحبا)", "(ابحرم)"),
        ("رقم 12.5 فقط", "طقف 12.5 مقر"),
    ],
)
def test_right_to_left_visual_order(logical: str, visual: str) -> None:
    assert visual_order(logical) == visual


def test_left_to_right_paragraph_with_arabic_data() -> None:
    assert visual_order("name: سارة 12", rtl=False) == "name: 12 ةراس"
    assert visual_order("‫ab‬", rtl=False) == "ab"


def test_shaping_uses_presentation_forms() -> None:
    shaped = shape("سارة")
    assert all("ﭐ" <= c <= "﻿" for c in shaped)
    assert unicodedata.normalize("NFKC", shaped) == "سارة"[::-1]


# ------------------------------------------------------------------ redaction policy


def _row(**overrides: Any) -> DecisionRecord:
    values: dict[str, Any] = {
        "id": new_decision_id(),
        "org_id": ORG,
        "workflow": "pay",
        "entity_type": "invoice",
        "entity_id": "inv-1",
        "kind": "risk_score",
        "replay_handler": "core.risk_combiner",
        "inputs": {"assessment_id": "a-1"},
        "policy_versions": [{"id": "p", "kind": "risk_policy", "version": 1, "hash": "h"}],
        "prompt_versions": [{"task": "core_extract", "version": "v1", "hash": "x"}],
        "model_call_ids": [uuid.UUID(int=9)],
        "model": {"provider": "fake"},
        "output": {"score": 0.7, "level": "high"},
        "confidence": 0.8,
        "evidence": {
            "risk_signals": [{"detector": "core.bank_change"}],
            "rule_results": [{"rule_id": "r1", "outcome": "fail"}],
            "workflow_specific": {"x": 1},
        },
        "final_action": "approve",
        "actor_type": "user",
        "actor_id": "user-1",
        "decided_at": datetime(2026, 10, 7, tzinfo=UTC),
        "created_at": datetime(2026, 10, 7, tzinfo=UTC),
        "audit_event_ids": [uuid.UUID(int=1)],
        "approval_ids": [],
        "supersedes": None,
        "content_hash": "c" * 64,
    }
    values.update(overrides)
    return DecisionRecord(**values)


SENSITIVE = {
    "fields": {"iban": "AE070331234567890123456"},
    "guardrail_evidence": {"f1": "ignore previous instructions"},
    "something_new": "x",
    "reason": "Called the vendor, bank details confirmed",
}


def _receipt(audience: Audience) -> dict[str, Any]:
    return receipt(_row(), ReceiptContext(sensitive=SENSITIVE, intact=True), audience)


def test_internal_full_sees_everything() -> None:
    data = _receipt(Audience.INTERNAL_FULL)
    assert data["personal_data"]["fields"]["iban"].startswith("AE07")
    assert data["internal_note"] == SENSITIVE["reason"]
    assert data["evidence"]["risk_signals"]
    assert data["redacted"] == []


def test_auditor_sees_everything_but_decrypted_personal_data() -> None:
    data = _receipt(Audience.INTERNAL)
    assert data["personal_data"] == {
        "fields": {"redacted": True},
        "guardrail_evidence": {"redacted": True},
        "something_new": {"redacted": True},
    }
    assert data["internal_note"] == {"redacted": True}
    assert "AE07" not in json.dumps(data)
    assert data["evidence"]["risk_signals"]
    assert data["model_call_ids"]
    assert set(data["redacted"]) == {
        "internal_note",
        "personal_data.fields",
        "personal_data.guardrail_evidence",
        "personal_data.something_new",
    }


def test_subject_sees_only_their_own_decision_without_internal_notes() -> None:
    row = _row()
    ctx = ReceiptContext(sensitive=SENSITIVE, intact=True)
    data = subject_receipt(row, ctx, SubjectRef("invoice", "inv-1"))
    assert data["audience"] == "subject"
    for omitted in ("internal_note", "actor", "links", "model", "model_call_ids", "redacted"):
        assert omitted not in data
    assert data["personal_data"] == {"fields": SENSITIVE["fields"]}
    assert data["evidence"] == {"rule_results": [{"rule_id": "r1", "outcome": "fail"}]}
    assert data["human_action"] == {"action": "approve"}
    with pytest.raises(NotFoundError):
        subject_receipt(row, ctx, SubjectRef("invoice", "inv-2"))


def test_policy_is_data_and_fails_closed_for_unknown_sections() -> None:
    assert visibility("brand_new_section", Audience.INTERNAL_FULL) is Visibility.OMIT
    assert visibility("evidence.unknown", Audience.SUBJECT) is Visibility.OMIT
    assert visibility("personal_data.unknown", Audience.INTERNAL) is Visibility.REDACT
    # Every rule names all three audiences, and subjects never get a redaction marker.
    for rule in RECEIPT_POLICY.values():
        assert set(rule) == set(Audience)
        assert rule[Audience.SUBJECT] is not Visibility.REDACT
    assert redact({"unknown": 1}, Audience.INTERNAL_FULL) == {
        "audience": "internal_full",
        "redacted": [],
    }


def test_appeal_creation_permission_per_workflow() -> None:
    def actor(*perms: str) -> Actor:
        return Actor(
            kind=ActorKind.USER,
            id=uuid.uuid4(),
            org_id=ORG,
            grants=(),
            permissions=frozenset(perms),
        )

    # The core-wide grant covers every workflow; a workflow's own grant covers it only.
    assert create_permission(actor(APPEAL_CREATE), "pay") == APPEAL_CREATE
    assert create_permission(actor("expense.appeal.create"), "expense") == "expense.appeal.create"
    assert create_permission(actor("expense.appeal.create"), "pay") is None
    assert create_permission(actor("expense.appeal.create"), "core") is None
    assert authorize_appeal_creation(actor("expense.appeal.create"), "expense") == (
        "expense.appeal.create"
    )
    with pytest.raises(PermissionDeniedError):
        authorize_appeal_creation(actor("expense.note.read"), "expense")


def test_audience_follows_permissions() -> None:
    def actor(*perms: str) -> Actor:
        return Actor(
            kind=ActorKind.USER,
            id=uuid.uuid4(),
            org_id=ORG,
            grants=(),
            permissions=frozenset(perms),
        )

    assert audience_for(actor(DECISION_READ, DECISION_PERSONAL_READ)) is Audience.INTERNAL_FULL
    assert audience_for(actor(DECISION_READ)) is Audience.INTERNAL
    with pytest.raises(NotFoundError):
        audience_for(actor())
    # A reader of the record's workflow (checked by the caller) gets the internal view.
    assert audience_for(actor(), workflow_reader=True) is Audience.INTERNAL
    assert audience_for(actor(DECISION_PERSONAL_READ), workflow_reader=True) is (
        Audience.INTERNAL_FULL
    )


@pytest.mark.parametrize("lang", ["en", "ar"])
def test_pdf_renders_in_both_languages(lang: str) -> None:
    pdf = render_pdf(_receipt(Audience.INTERNAL), lang=lang)
    assert pdf.startswith(b"%PDF")
    with pdfplumber.open(io.BytesIO(pdf)) as doc:
        text = unicodedata.normalize("NFKC", "\n".join(p.extract_text() or "" for p in doc.pages))
    if lang == "en":
        assert "Decision receipt" in text
        assert "[redacted]" in text
    else:
        assert "إيصال القرار"[::-1] in text
        assert "محجوب"[::-1] in text
    with pytest.raises(ValueError, match="lang"):
        render_pdf({}, lang="fr")


# ------------------------------------------------------------------ anchors


def _seed() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()


def test_signing_key_loading_fails_closed() -> None:
    with pytest.raises(ConfigError, match=SIGNING_KEY_ENV):
        load_signing_key({})
    with pytest.raises(ConfigError, match="32 bytes"):
        load_signing_key({SIGNING_KEY_ENV: base64.urlsafe_b64encode(b"x" * 16).decode()})
    with pytest.raises(ConfigError, match="placeholder"):
        load_signing_key({SIGNING_KEY_ENV: base64.urlsafe_b64encode(b"\0" * 32).decode()})
    seed = _seed()
    key = load_signing_key({SIGNING_KEY_ENV: seed})
    public = encode_key(public_bytes(key.public_key()))
    assert load_signing_key({SIGNING_KEY_ENV: seed, PUBLIC_KEY_ENV: public})
    other = encode_key(public_bytes(Ed25519PrivateKey.generate().public_key()))
    with pytest.raises(ConfigError, match="does not match"):
        load_signing_key({SIGNING_KEY_ENV: seed, PUBLIC_KEY_ENV: other})
    assert load_public_key({}) is None
    loaded = load_public_key({PUBLIC_KEY_ENV: public})
    assert loaded is not None
    assert public_bytes(loaded) == public_bytes(key.public_key())


def test_anchor_documents_verify_and_detect_tampering() -> None:
    key = Ed25519PrivateKey.generate()
    keys = public_keys(key.public_key())
    data = sign_head(key, org_id=ORG, seq=7, head_hash="a" * 64, at=datetime.now(UTC))
    head = verify_head(data, keys)
    assert (head.org_id, head.seq, head.hash) == (ORG, 7, "a" * 64)
    doc = json.loads(data)
    for field, value in (("seq", 8), ("hash", "b" * 64), ("org_id", str(uuid.uuid4()))):
        with pytest.raises(AnchorError):
            verify_head(json.dumps(doc | {field: value}).encode(), keys)
    with pytest.raises(AnchorError, match="no signature"):
        verify_head(json.dumps({k: v for k, v in doc.items() if k != "signature"}).encode(), keys)
    with pytest.raises(AnchorError):
        verify_head(b"not json", keys)
    with pytest.raises(AnchorError):
        sign_head(key, org_id=ORG, seq=0, head_hash="a" * 64, at=datetime.now(UTC))


async def test_in_memory_anchor_store_is_write_once() -> None:
    store = InMemoryAnchorStore()
    key = anchor_key(ORG, 3)
    assert key.endswith("/00000000000000000003.json")
    assert await store.put_new(key, b"1")
    assert not await store.put_new(key, b"2")
    assert await store.get(key) == b"1"
    assert await store.list_keys(f"audit-anchors/v1/{ORG}/") == [key]
    with pytest.raises(ValueError, match="anchor key"):
        await store.put_new("other/key.json", b"x")


def _chain(n: int) -> list[AuditRecord]:
    records: list[AuditRecord] = []
    prev = GENESIS_HASH
    for seq in range(1, n + 1):
        record = AuditRecord(
            id=uuid.UUID(int=seq),
            org_id=ORG,
            seq=seq,
            occurred_at=datetime(2026, 10, 7, tzinfo=UTC),
            actor_type="system",
            actor_id="t",
            action="core.test.done",
            entity_type=None,
            entity_id=None,
            workflow=None,
            payload={"i": seq},
            request_id=None,
            prev_hash=prev,
            hash="",
        )
        digest = compute_hash(prev, record.hashed_fields())
        records.append(AuditRecord(**_with_hash(record, digest)))
        prev = digest
    return records


def _with_hash(record: AuditRecord, digest: str) -> dict[str, Any]:
    from dataclasses import asdict

    return asdict(record) | {"hash": digest}


def test_verify_records_checks_anchors() -> None:
    chain = _chain(5)
    anchors = {3: chain[2].hash, 5: chain[4].hash}
    assert verify_records(ORG, chain, anchors=anchors).ok
    truncated = verify_records(ORG, chain[:4], anchors=anchors)
    assert (truncated.ok, truncated.reason, truncated.broken_seq) == (False, "anchor_truncated", 5)
    wrong = verify_records(ORG, chain, anchors={3: "f" * 64})
    assert (wrong.ok, wrong.reason, wrong.broken_seq) == (False, "anchor_mismatch", 3)
