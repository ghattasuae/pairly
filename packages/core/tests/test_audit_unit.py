# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Audit log without a database: canonical form, hashing, payload rules, chain checks."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from pairly_core.audit import (
    GENESIS_HASH,
    ActorType,
    AuditAction,
    AuditError,
    AuditFilter,
    AuditRecord,
    ChainVerifier,
    _entity_type_from_class,
    append_audit,
    canonical_event_json,
    canonical_json,
    compute_hash,
    csv_row,
    csv_safe,
    format_timestamp,
    validate_payload,
    verify_records,
)
from pairly_core.db import TenantError, create_sessionmaker, set_tenant
from pairly_core.rbac import (
    Actor,
    ActorKind,
    PermissionDeniedError,
    RoleGrant,
    authorize,
    build_registry,
)

ORG = uuid.UUID("11111111-1111-4111-8111-111111111111")
T0 = datetime(2026, 10, 7, 8, 30, 0, 123456, tzinfo=UTC)


def _chain(n: int, org: uuid.UUID = ORG) -> list[AuditRecord]:
    records: list[AuditRecord] = []
    prev = GENESIS_HASH
    for seq in range(1, n + 1):
        record = AuditRecord(
            id=uuid.UUID(int=seq),
            org_id=org,
            seq=seq,
            occurred_at=T0 + timedelta(seconds=seq),
            actor_type="user",
            actor_id=str(uuid.UUID(int=1000 + seq)),
            action="core.test.done",
            entity_type="thing",
            entity_id=f"thing-{seq}",
            workflow=None,
            payload={"n": seq, "label": "ünïcode"},
            request_id=None,
            prev_hash=prev,
            hash="",
        )
        record = dataclasses.replace(record, hash=compute_hash(prev, record.hashed_fields()))
        records.append(record)
        prev = record.hash
    return records


# ------------------------------------------------------------------ canonical form


def test_hash_definition_matches_documented_recipe() -> None:
    record = _chain(1)[0]
    body = json.dumps(
        record.hashed_fields(),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    expected = hashlib.sha256(bytes.fromhex(GENESIS_HASH) + body).hexdigest()
    assert record.hash == expected
    assert canonical_event_json(record.hashed_fields()) == body
    # Fixed bytes: any change to the canonical form must be deliberate.
    assert body.startswith(b'{"action":"core.test.done","actor_id":')
    assert b'"occurred_at":"2026-10-07T08:30:01.123456Z"' in body
    assert "ünïcode".encode() in body


def test_canonical_json_is_order_independent() -> None:
    assert canonical_json({"b": 1, "a": [1, {"d": None, "c": True}]}) == (
        b'{"a":[1,{"c":true,"d":null}],"b":1}'
    )


def test_canonical_event_json_requires_exact_fields() -> None:
    fields = _chain(1)[0].hashed_fields()
    with pytest.raises(AuditError):
        canonical_event_json({**fields, "extra": 1})
    fields.pop("request_id")
    with pytest.raises(AuditError):
        canonical_event_json(fields)


def test_compute_hash_rejects_bad_prev_hash() -> None:
    fields = _chain(1)[0].hashed_fields()
    for bad in ("0" * 63, "G" * 64, "A" * 64):
        with pytest.raises(AuditError):
            compute_hash(bad, fields)


def test_format_timestamp() -> None:
    plus4 = timezone(timedelta(hours=4))
    assert format_timestamp(datetime(2026, 1, 2, 7, 0, tzinfo=plus4)) == (
        "2026-01-02T03:00:00.000000Z"
    )
    with pytest.raises(AuditError):
        format_timestamp(datetime(2026, 1, 2))


# ------------------------------------------------------------------ payload rules


def test_payload_accepts_plain_json() -> None:
    data = {"ids": ("a", "b"), "count": 3, "ok": True, "none": None, "nested": {"k": "v"}}
    assert validate_payload(data) == {
        "ids": ["a", "b"],
        "count": 3,
        "ok": True,
        "none": None,
        "nested": {"k": "v"},
    }
    assert validate_payload(None) == {}


@pytest.mark.parametrize(
    "payload",
    [
        {"amount": 1.5},
        {"text": "nul\x00byte"},
        {"password": "x"},
        {"api_key": "x"},
        {"user": {"refresh_token": "x"}},
        {"Authorization": "Bearer x"},
        {"iban": "AE07..."},
        {"bad key": 1},
        {"big": 2**64},
        {"long": "x" * 2001},
        {"obj": object()},
        {"deep": {"a": {"b": {"c": {"d": {"e": {"f": {"g": {"h": {"i": 1}}}}}}}}}},
        {f"k{i}": "x" * 1000 for i in range(20)},
    ],
    ids=lambda p: next(iter(p)) if isinstance(p, dict) and p else "x",
)
def test_payload_rejects_unsafe_values(payload: dict[str, Any]) -> None:
    with pytest.raises(AuditError):
        validate_payload(payload)


def test_payload_must_be_object() -> None:
    with pytest.raises(AuditError):
        validate_payload(["not", "an", "object"])  # type: ignore[arg-type]


def test_payload_allows_harmless_token_like_keys() -> None:
    assert validate_payload({"token_count": 10, "iban_last4": "1234"})


# ------------------------------------------------------------------ chain verification


def test_valid_chain_verifies() -> None:
    chain = _chain(5)
    assert chain[0].prev_hash == GENESIS_HASH
    result = verify_records(ORG, chain)
    assert result.ok
    assert (result.checked, result.last_seq, result.last_hash) == (5, 5, chain[-1].hash)
    assert result.to_json()["broken_seq"] is None


def test_empty_chain_is_ok() -> None:
    result = verify_records(ORG, [])
    assert result.ok
    assert (result.checked, result.last_seq, result.last_hash) == (0, 0, GENESIS_HASH)


def test_edited_field_is_detected_at_its_seq() -> None:
    chain = _chain(5)
    chain[2] = dataclasses.replace(chain[2], payload={"n": 999, "label": "ünïcode"})
    result = verify_records(ORG, chain)
    assert (result.ok, result.broken_seq, result.reason) == (False, 3, "hash_mismatch")
    assert result.checked == 2


def test_rehashed_edit_breaks_the_next_link() -> None:
    chain = _chain(5)
    edited = dataclasses.replace(chain[2], action="core.test.forged")
    chain[2] = dataclasses.replace(
        edited, hash=compute_hash(edited.prev_hash, edited.hashed_fields())
    )
    result = verify_records(ORG, chain)
    assert (result.broken_seq, result.reason) == (4, "prev_hash_mismatch")


def test_removed_event_is_detected() -> None:
    chain = _chain(5)
    del chain[1]
    result = verify_records(ORG, chain)
    assert (result.broken_seq, result.reason) == (2, "missing_event")


def test_reordered_and_foreign_events_are_detected() -> None:
    chain = _chain(3)
    assert verify_records(ORG, [chain[0], chain[0]]).reason == "seq_out_of_order"
    other = _chain(1, org=uuid.uuid4())
    assert verify_records(ORG, other).reason == "org_mismatch"
    bad_prev = [dataclasses.replace(chain[0], prev_hash="f" * 64)]
    assert verify_records(ORG, bad_prev).reason == "prev_hash_mismatch"
    unhashable = [dataclasses.replace(chain[0], payload={"x": float("nan")})]
    assert verify_records(ORG, unhashable).reason == "unhashable_event"


def test_verifier_stops_after_first_break() -> None:
    chain = _chain(3)
    verifier = ChainVerifier(ORG)
    assert verifier.feed(chain[0])
    assert not verifier.feed(chain[2])
    assert not verifier.feed(chain[1])
    assert verifier.result().broken_seq == 2


def test_json_export_round_trip_verifies() -> None:
    chain = _chain(4)
    exported = json.loads(canonical_json([r.to_json() for r in chain]))
    parsed = [AuditRecord.from_json(item) for item in exported]
    assert parsed == chain
    assert verify_records(ORG, parsed).ok


# ------------------------------------------------------------------ exports and helpers


def test_csv_cells_are_formula_safe() -> None:
    assert csv_safe("=HYPERLINK()") == "'=HYPERLINK()"
    assert csv_safe("-1") == "'-1"
    assert csv_safe("plain") == "plain"
    record = dataclasses.replace(_chain(1)[0], entity_id="@evil")
    row = csv_row(record)
    assert "'@evil" in row
    assert row[0] == "1"


def test_filter_clauses() -> None:
    assert AuditFilter().clauses() == []
    flt = AuditFilter(action="a.b", actor_type="user", occurred_from=T0, occurred_to=T0)
    assert len(flt.clauses()) == 4


def test_entity_type_from_class() -> None:
    assert _entity_type_from_class("InvoiceLine") == "invoice_line"
    assert _entity_type_from_class(None) is None
    assert _entity_type_from_class("_") is None


# ------------------------------------------------------------------ authorize denials


def _actor(kind: ActorKind, *roles: str) -> Actor:
    registry = build_registry()
    grants = tuple(RoleGrant(r) for r in roles)
    return Actor(kind, uuid.uuid4(), ORG, grants, registry.resolve(grants))


@dataclasses.dataclass(frozen=True)
class Thing:
    id: uuid.UUID
    org_id: uuid.UUID


def test_denial_carries_audit_context() -> None:
    actor = _actor(ActorKind.USER, "read_only")
    with pytest.raises(PermissionDeniedError) as missing:
        authorize(actor, "core.users.manage")
    assert missing.value.denial is not None
    assert (missing.value.denial.reason, missing.value.denial.resource_id) == (
        "missing_permission",
        None,
    )

    own = Thing(uuid.uuid4(), ORG)
    with pytest.raises(PermissionDeniedError) as on_own:
        authorize(actor, "core.users.manage", own)
    assert on_own.value.denial is not None
    assert on_own.value.denial.resource_id == str(own.id)
    assert on_own.value.denial.resource_type == "Thing"

    admin = _actor(ActorKind.USER, "admin")
    foreign = Thing(uuid.uuid4(), uuid.uuid4())
    with pytest.raises(PermissionDeniedError) as outside:
        authorize(admin, "core.users.read", foreign)
    assert outside.value.denial is not None
    assert outside.value.denial.reason == "resource_outside_org"
    # Another tenant's id is never copied into this tenant's audit log.
    assert outside.value.denial.resource_id is None


def test_agent_denial_reason() -> None:
    agent = Actor(
        ActorKind.AGENT,
        uuid.uuid4(),
        ORG,
        (RoleGrant("admin"),),
        build_registry().resolve((RoleGrant("admin"),)),
    )
    with pytest.raises(PermissionDeniedError) as denied:
        authorize(agent, "core.users.manage")
    assert denied.value.denial is not None
    assert denied.value.denial.reason == "agent_action_not_allowed"


# ------------------------------------------------------------------ append_audit guards


def _session(org: uuid.UUID | None) -> Any:
    engine = create_async_engine("postgresql+asyncpg://nobody:pw@127.0.0.1:1/none")
    session = create_sessionmaker(engine)()
    set_tenant(session, org, None)
    return session


async def test_append_requires_tenant_and_valid_fields() -> None:
    with pytest.raises(TenantError):
        await append_audit(_session(None), action="core.x.y", actor_type="user", actor_id="u")
    with pytest.raises(TenantError):
        await append_audit(
            _session(ORG), action="core.x.y", actor_type="user", actor_id="u", org_id=uuid.uuid4()
        )
    bad_calls: list[dict[str, Any]] = [
        {"action": "Not An Action", "actor_type": "user", "actor_id": "u"},
        {"action": "single", "actor_type": "user", "actor_id": "u"},
        {"action": "core.x.y", "actor_type": "robot", "actor_id": "u"},
        {"action": "core.x.y", "actor_type": "user", "actor_id": "has space"},
        {"action": "core.x.y", "actor_type": "user", "actor_id": "u", "workflow": "Pay!"},
        {"action": "core.x.y", "actor_type": "user", "actor_id": "u", "entity_type": "X"},
        {"action": "core.x.y", "actor_type": "user", "actor_id": "u", "request_id": "a b"},
        {"action": "core.x.y", "actor_type": "user", "actor_id": "u", "payload": {"x": 1.0}},
    ]
    for kwargs in bad_calls:
        with pytest.raises(AuditError):
            await append_audit(_session(ORG), **kwargs)


def test_action_names_are_valid() -> None:
    names = [v for k, v in vars(AuditAction).items() if k.isupper()]
    assert names
    assert all(name.startswith("core.") for name in names)
    assert ActorType("system") is ActorType.SYSTEM
