# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""The expense plugin without a database: discovery, permissions, policies as code, what
counts as a usable value, and the synthetic files being up to date."""

from __future__ import annotations

import datetime as dt
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from pairly_core.approvals.rules import NoMatchingChainError, parse_rules
from pairly_core.extraction.models import Extraction
from pairly_core.extraction.service import StoredCorrection, StoredExtraction, StoredField
from pairly_core.plugins import demo_seeds, discover_plugins, plugin_routers
from pairly_core.policy.rules import Outcome, evaluate_rule_set, parse_rule_set
from pairly_core.rbac import (
    AGENT_ALLOWED_ACTIONS,
    BuiltinRole,
    RoleGrant,
    build_registry,
    permission_action,
)
from pairly_core.risk.combiner import parse_risk_policy
from pairly_core.workflow.runtime import WorkflowRuntime
from pairly_expense.notes import receipt_values, restore_result
from pairly_expense.permissions import NOTE_APPROVE, NOTE_SUBMIT, PERMISSIONS
from pairly_expense.plugin import plugin
from pairly_expense.policies import APPROVAL_RULES_V1, RISK_V1, RULES_V1
from synth.expense.__main__ import outputs

AS_OF = dt.date(2026, 10, 8)


def test_discovered_through_the_entry_point() -> None:
    plugins = discover_plugins()
    assert plugins["expense"] is plugin
    assert [pid for pid, _ in plugin_routers([plugin])] == ["expense"]
    assert set(demo_seeds([plugin])) == {"expense-demo"}
    registry = build_registry(list(plugins.values()))
    runtime = WorkflowRuntime.from_plugins(
        [plugin],
        sessionmaker=None,
        checkpointer=None,
        registry=registry,  # type: ignore[arg-type]
    )
    spec = runtime.spec("expense", "expense_note")
    assert spec.start_permission == NOTE_SUBMIT
    assert runtime.approval_type("expense.note") is not None


def test_permissions_and_role_grants() -> None:
    registry = build_registry([plugin])
    assert all(p.name.startswith("expense.") for p in PERMISSIONS)
    agent = registry.permissions_for_role(BuiltinRole.AGENT)
    expense_agent = {p for p in agent if p.startswith("expense.")}
    assert expense_agent
    assert {permission_action(p) for p in expense_agent} <= AGENT_ALLOWED_ACTIONS
    # Only approvers and finance controllers approve; the admin does not by default.
    approvers = {r for r in BuiltinRole if NOTE_APPROVE in registry.permissions_for_role(r)}
    assert approvers == {BuiltinRole.APPROVER, BuiltinRole.FINANCE_CONTROLLER}
    # A membership limited to expense grants expense permissions only.
    scoped = registry.resolve([RoleGrant("employee", None)])
    assert NOTE_SUBMIT in scoped
    assert all(p.startswith("expense.") for p in scoped)


def test_policies_parse() -> None:
    assert parse_rule_set(RULES_V1).name == "expense.note"
    assert parse_risk_policy(RISK_V1).name == "expense.note"
    assert parse_rules(APPROVAL_RULES_V1).approval_type == "expense.note"


@pytest.mark.parametrize(
    ("amount_aed", "chain"),
    [("0.01", "under_500"), ("499.99", "under_500"), ("500", "from_500"), (None, "from_500")],
)
def test_approval_chain_by_amount(amount_aed: str | None, chain: str) -> None:
    rules = parse_rules(APPROVAL_RULES_V1)
    assert rules.select_chain({"amount_aed": amount_aed}).name == chain
    steps = [s.name for s in rules.select_chain({"amount_aed": amount_aed}).steps]
    assert steps == (["approver"] if chain == "under_500" else ["approver", "finance"])


def test_approval_rules_never_match_nothing() -> None:
    rules = parse_rules(APPROVAL_RULES_V1)
    try:
        rules.select_chain({})
    except NoMatchingChainError:  # pragma: no cover - the last chain is the default
        pytest.fail("the default chain must match anything")


def _rules(facts: dict[str, Any]) -> dict[str, Outcome]:
    evaluation = evaluate_rule_set(parse_rule_set(RULES_V1), facts, as_of=AS_OF)
    return {r.rule_id: r.outcome for r in evaluation.results}


CLEAN_FACTS: dict[str, Any] = {
    "amount": Decimal("81.90"),
    "currency": "AED",
    "category": "meals",
    "is_weekend": False,
    "receipt_evidenced": True,
}


def test_expense_rules() -> None:
    assert set(_rules(CLEAN_FACTS).values()) == {Outcome.PASS}
    unread = _rules({**CLEAN_FACTS, "amount": Decimal("250"), "receipt_evidenced": False})
    assert unread["receipt_required"] is Outcome.FAIL
    assert _rules({**CLEAN_FACTS, "amount": Decimal("300.01")})["meals_limit"] is (
        Outcome.NEEDS_REVIEW
    )
    assert _rules({**CLEAN_FACTS, "is_weekend": True})["weekend_review"] is Outcome.NEEDS_REVIEW
    assert _rules({**CLEAN_FACTS, "currency": "USD"})["currency_aed"] is Outcome.NEEDS_REVIEW
    # A value that was not read is an error (a person looks), never a pass.
    missing = _rules({"receipt_evidenced": False})
    assert missing["receipt_required"] is Outcome.ERROR
    assert missing["weekend_review"] is Outcome.ERROR


def _stored(fields: dict[str, tuple[Any, list[str]]], *, corrections: Any = ()) -> StoredExtraction:
    row = Extraction(
        id=uuid.uuid4(),
        org_id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        workflow="expense",
        schema_name="expense.receipt",
        schema_version="1",
        field_specs={},
        prompt_task="core_extract",
        prompt_version="v1",
        prompt_hash="0" * 64,
        model_provider="anthropic",
        model_name="m",
        model_call_ids=[],
        model_chain_id=None,
        from_recording=True,
        status="ok",
        reasons=[],
        ignored_paths=[],
    )
    stored_fields = [
        StoredField(
            path=path,
            pattern=path,
            kind="text",
            value=value,
            raw_value=None,
            confidence=0.0 if flags else 0.95,
            threshold=0.8,
            required=path != "description",
            needs_correction=bool(flags),
            flags=flags,
            spans=[],
            position=i,
        )
        for i, (path, (value, flags)) in enumerate(fields.items())
    ]
    return StoredExtraction(row=row, fields=stored_fields, corrections=list(corrections))


READ: dict[str, tuple[Any, list[str]]] = {
    "merchant": ("Al Noor Cafe", []),
    "date": ("2026-10-03", []),
    "total": ({"amount": "81.90", "currency": "AED"}, []),
    "currency": ("AED", []),
    "category": ("meals", []),
    "description": (None, []),
}


def test_receipt_values_keep_only_usable_values() -> None:
    values = receipt_values(_stored(READ))
    assert (values.amount, values.amount_aed, values.evidenced) == (
        Decimal("81.90"),
        Decimal("81.90"),
        True,
    )
    assert values.facts()["is_weekend"] is True  # a Saturday
    flagged = receipt_values(_stored({**READ, "total": (READ["total"][0], ["low_confidence"])}))
    assert (flagged.amount, flagged.evidenced) == (None, False)
    assert "amount" not in flagged.facts()
    mismatch = receipt_values(_stored({**READ, "currency": ("USD", [])}))
    assert (mismatch.amount, mismatch.currency) == (None, None)
    correction = StoredCorrection(
        id=uuid.uuid4(),
        extraction_id=uuid.uuid4(),
        field_path="total",
        old_value=None,
        new_value={"amount": "90.00", "currency": "AED"},
        corrected_by=uuid.uuid4(),
        reason="Read from the paper copy",
        created_at=datetime.now(UTC),
    )
    corrected = _stored({**READ, "total": (None, ["missing"])}, corrections=[correction])
    assert receipt_values(corrected).amount == Decimal("90.00")
    assert receipt_values(None).facts() == {"receipt_evidenced": False}


def test_restore_result_marks_failed_extractions() -> None:
    stored = _stored({k: (None, ["missing"]) for k in READ})
    stored.row.reasons = ["model_unavailable:recording_missing"]
    stored.row.status = "needs_human"
    assert restore_result(stored).data is None
    ok = restore_result(_stored(READ))
    assert ok.data is not None
    assert ok.data.total.value is not None


def test_synthetic_receipts_and_recordings_are_up_to_date() -> None:
    for path, data in outputs().items():
        assert path.read_bytes() == data, f"{path} is stale: run uv run python -m synth.expense"


def test_migrations_are_their_own_branch() -> None:
    from alembic.script import ScriptDirectory

    core = plugin.migrations_dir.parents[3] / "apps" / "api" / "alembic" / "versions"
    script = ScriptDirectory(
        str(core.parent), version_locations=[str(core), str(plugin.migrations_dir)]
    )
    first = script.get_revision("expense_0001")
    assert first is not None
    assert first.branch_labels == {"expense"}
    assert first.down_revision is None
    assert first.dependencies == "0015"
    newest = sorted(p.stem for p in plugin.migrations_dir.glob("expense_*.py"))[-1]
    assert newest.startswith(script.get_revisions("expense@head")[0].revision)
