# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Unit tests (no database): approval rules, graph specs, payload hashing, plugin graphs."""

from __future__ import annotations

import sys
import types
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from importlib.metadata import EntryPoint
from typing import Any

import pytest
from langgraph.graph import StateGraph

from pairly_core import plugins
from pairly_core.approvals.rules import (
    NoMatchingChainError,
    lookup,
    parse_rules,
    rules_validator,
)
from pairly_core.approvals.service import (
    InvalidPayloadError,
    canonical_payload,
    payload_hash,
    request_hash,
)
from pairly_core.policy_versions import PolicyError, content_hash
from pairly_core.workflow.checkpoint import org_of_thread, psycopg_conninfo
from pairly_core.workflow.models import thread_id_for
from pairly_core.workflow.runtime import _error_text, node_retry
from pairly_core.workflow.spec import ApprovalType, GraphSpec, SpecError

RULES = """\
schema: pairly.approval_rules/v1
approval_type: pay.invoice
sla_hours: 24
warn_hours: 30
escalation:
  roles: [finance_controller]
chains:
  - name: small
    when:
      - {field: amount, op: lte, value: 1000.50}
      - {field: vendor.country, op: in, value: [AE, SA]}
    steps:
      - {name: manager, roles: [approver]}
  - name: flagged
    when:
      - {field: risk, op: eq, value: high}
    steps:
      - {name: controller, mode: all_of, roles: [finance_controller], sla_hours: 4}
  - name: default
    steps:
      - {name: manager, roles: [approver]}
"""


def test_chain_selection() -> None:
    rules = parse_rules(RULES)
    assert rules.select_chain({"amount": "1000.5", "vendor": {"country": "AE"}}).name == "small"
    assert rules.select_chain({"amount": "1000.51", "vendor": {"country": "AE"}}).name == "default"
    assert rules.select_chain({"amount": "10", "vendor": {"country": "US"}}).name == "default"
    assert rules.select_chain({"amount": "oops", "risk": "high"}).name == "flagged"
    assert rules.select_chain({}).name == "default"
    flagged = rules.chains[1]
    assert rules.step_sla_seconds(flagged.steps[0]) == 4 * 3600
    # warn_hours larger than the SLA is capped at the SLA.
    assert rules.warn_seconds(4 * 3600) == 4 * 3600


def test_no_default_chain_fails_closed() -> None:
    rules = parse_rules(
        "schema: pairly.approval_rules/v1\napproval_type: pay.invoice\nchains:\n"
        "  - name: only\n    when: [{field: amount, op: gt, value: '5'}]\n"
        "    steps: [{name: a, roles: [approver]}]\n"
    )
    with pytest.raises(NoMatchingChainError):
        rules.select_chain({"amount": "1"})
    with pytest.raises(NoMatchingChainError):
        rules.select_chain({"amount": True})


@pytest.mark.parametrize(
    ("op", "value", "actual", "expected"),
    [
        ("eq", "7", 7, True),
        ("ne", "x", "y", True),
        ("gte", "7", "7.00", True),
        ("lt", "7", "6.99", True),
        ("not_in", ["a", "b"], "c", True),
        ("in", ["a"], "b", False),
        ("eq", True, True, True),
        ("eq", True, "true", False),
    ],
)
def test_conditions(op: str, value: Any, actual: Any, expected: bool) -> None:
    rules = parse_rules(
        "schema: pairly.approval_rules/v1\napproval_type: pay.invoice\nchains:\n"
        f"  - name: c\n    when: [{{field: f, op: {op}, value: {value!r}}}]\n"
        "    steps: [{name: a, roles: [approver]}]\n"
        "  - name: d\n    steps: [{name: a, roles: [approver]}]\n".replace("'", '"').replace(
            "True", "true"
        )
    )
    assert (rules.select_chain({"f": actual}).name == "c") is expected


@pytest.mark.parametrize(
    "bad",
    [
        "not: [valid",
        "- a list",
        "schema: other\napproval_type: pay.invoice\nchains: []",
        "schema: pairly.approval_rules/v1\napproval_type: pay.invoice\nchains: []",
        # a chain without conditions that is not last
        "schema: pairly.approval_rules/v1\napproval_type: pay.invoice\nchains:\n"
        "  - {name: a, steps: [{name: s, roles: [approver]}]}\n"
        "  - {name: b, steps: [{name: s, roles: [approver]}]}\n",
        # a step with no approvers
        "schema: pairly.approval_rules/v1\napproval_type: pay.invoice\nchains:\n"
        "  - {name: a, steps: [{name: s}]}\n",
        # a numeric operator with a word
        "schema: pairly.approval_rules/v1\napproval_type: pay.invoice\nchains:\n"
        "  - {name: a, when: [{field: x, op: gt, value: big}], steps: [{name: s, users: []}]}\n",
        # in without a list
        "schema: pairly.approval_rules/v1\napproval_type: pay.invoice\nchains:\n"
        "  - {name: a, when: [{field: x, op: in, value: a}], steps: [{name: s, roles: [r]}]}\n"
        "  - {name: b, steps: [{name: s, roles: [r]}]}\n",
        # eq with a list
        "schema: pairly.approval_rules/v1\napproval_type: pay.invoice\nchains:\n"
        "  - {name: a, when: [{field: x, op: eq, value: [a]}], steps: [{name: s, roles: [r]}]}\n"
        "  - {name: b, steps: [{name: s, roles: [r]}]}\n",
        # unknown key (extra forbidden), duplicate chain and step names
        "schema: pairly.approval_rules/v1\napproval_type: pay.invoice\nauto_approve: true\n"
        "chains: [{name: a, steps: [{name: s, roles: [r]}]}]\n",
        "schema: pairly.approval_rules/v1\napproval_type: pay.invoice\nchains:\n"
        "  - {name: a, when: [{field: x, op: eq, value: 1}], steps: [{name: s, roles: [r]}]}\n"
        "  - {name: a, steps: [{name: s, roles: [r]}]}\n",
        "schema: pairly.approval_rules/v1\napproval_type: pay.invoice\nchains:\n"
        "  - {name: a, steps: [{name: s, roles: [r]}, {name: s, roles: [r]}]}\n",
    ],
)
def test_invalid_rules_are_refused(bad: str) -> None:
    with pytest.raises(PolicyError):
        parse_rules(bad)


def test_rules_validator_checks_the_type() -> None:
    text = (
        "schema: pairly.approval_rules/v1\napproval_type: pay.invoice\n"
        "chains: [{name: a, steps: [{name: s, roles: [r]}]}]\n"
    )
    assert rules_validator("pay.invoice")(text).approval_type == "pay.invoice"
    with pytest.raises(PolicyError, match="another approval type"):
        rules_validator("hire.offer")(text)


def test_lookup() -> None:
    assert lookup({"a": {"b": 1}}, "a.b") == 1
    assert lookup({"a": 1}, "a.b") is None
    assert lookup({}, "a") is None


def test_payload_hash_is_canonical() -> None:
    a = payload_hash({"b": "2", "a": {"y": 1, "x": [True, None]}})
    b = payload_hash({"a": {"x": [True, None], "y": 1}, "b": "2"})
    assert a == b
    assert a != payload_hash({"b": "3", "a": {"y": 1, "x": [True, None]}})
    data, _ = canonical_payload({"t": ("x", "y")})
    assert data == {"t": ["x", "y"]}
    for bad in ({"amount": 1.5}, {"x": object()}, {1: "a"}, {"s": "a\x00"}, "nope"):
        with pytest.raises(InvalidPayloadError):
            canonical_payload(bad)  # type: ignore[arg-type]
    with pytest.raises(InvalidPayloadError):
        canonical_payload({"big": "x" * 40000})
    deep: dict[str, Any] = {}
    node = deep
    for _ in range(20):
        node["n"] = {}
        node = node["n"]
    with pytest.raises(InvalidPayloadError):
        canonical_payload(deep)
    assert request_hash(uuid.UUID(int=1), "approve", None) == request_hash(
        uuid.UUID(int=1), "approve", ""
    )
    assert content_hash("a") != content_hash("b")


def test_thread_ids() -> None:
    org, run = uuid.uuid4(), uuid.uuid4()
    assert org_of_thread(thread_id_for(org, run)) == org
    assert org_of_thread("nope") is None
    assert org_of_thread("nope:x") is None
    assert org_of_thread(f"{org}:") is None
    assert psycopg_conninfo("postgresql+asyncpg://u:p@h:5/db") == "postgresql://u:p@h:5/db"


def test_error_text_never_leaks_messages() -> None:
    from pairly_core.approvals.executor import ExecutorRefusedError
    from pairly_core.errors import BadRequestError

    assert _error_text(RuntimeError("secret jane@example.test")) == "RuntimeError"
    assert _error_text(ExecutorRefusedError("x")) == "executor_refused: x"
    assert _error_text(BadRequestError("bad")) == "bad_request: bad"
    retry = node_retry()
    assert callable(retry.retry_on)
    assert not retry.retry_on(BadRequestError("x"))  # type: ignore[operator]
    assert not retry.retry_on(ValueError("x"))  # type: ignore[operator]
    assert retry.retry_on(ConnectionError("x"))  # type: ignore[operator]


# ------------------------------------------------------------------ specs


def _build() -> StateGraph[Any, Any, Any, Any]:
    raise NotImplementedError


def _spec(**overrides: Any) -> GraphSpec:
    args: dict[str, Any] = {
        "workflow": "pay",
        "name": "invoice",
        "version": 1,
        "build": _build,
        "start_permission": "pay.invoice.propose",
        "read_permission": "pay.invoice.read",
        "approval_types": (ApprovalType("pay.invoice", "pay.invoice.approve", RULES),),
    } | overrides
    return GraphSpec(**args)


def test_spec_validation() -> None:
    spec = _spec()
    assert spec.key == ("pay", "invoice")
    assert spec.approval_type("pay.invoice") is not None
    assert spec.approval_type("pay.other") is None
    for overrides in (
        {"name": "Bad Name"},
        {"version": 0},
        {"start_permission": "pay.invoice.approve"},
        {"start_permission": "hire.invoice.propose"},
        {"read_permission": "pay.invoice.list"},
        {"read_permission": "bad"},
        {"final_actions": {"Bad": _build}},
        {"approval_types": (ApprovalType("hire.offer", "hire.offer.approve", RULES),)},
    ):
        with pytest.raises(SpecError):
            _spec(**overrides)
    for args in (
        ("payinvoice", "pay.invoice.approve"),
        ("pay.invoice", "pay.invoice.read"),
        ("pay.invoice", "hire.invoice.approve"),
        ("pay.invoice", "bad"),
    ):
        with pytest.raises(SpecError):
            ApprovalType(args[0], args[1], RULES)


_FAKE = "_pairly_fake_graph_plugins"


@dataclass(frozen=True)
class _Plugin:
    id: str
    name: str
    permissions: tuple[object, ...] = ()
    graphs: Any = ()


@pytest.fixture
def fake_module() -> Iterator[types.ModuleType]:
    module = types.ModuleType(_FAKE)
    module.good = _Plugin("pay", "Pay", graphs=(_spec(),))  # type: ignore[attr-defined]
    module.str_graphs = _Plugin("pay", "Pay", graphs="x")  # type: ignore[attr-defined]
    module.not_spec = _Plugin("pay", "Pay", graphs=("x",))  # type: ignore[attr-defined]
    module.foreign = _Plugin("hire", "Hire", graphs=(_spec(),))  # type: ignore[attr-defined]
    module.duplicate = _Plugin("pay", "Pay", graphs=(_spec(), _spec()))  # type: ignore[attr-defined]
    sys.modules[_FAKE] = module
    yield module
    del sys.modules[_FAKE]


@pytest.mark.usefixtures("fake_module")
@pytest.mark.parametrize(
    ("name", "attr", "message"),
    [
        ("pay", "good", None),
        ("pay", "str_graphs", "graphs must be a sequence"),
        ("pay", "not_spec", "graphs must be GraphSpec"),
        ("hire", "foreign", "belongs to another workflow"),
        ("pay", "duplicate", "duplicate graph"),
    ],
)
def test_plugin_graphs_are_validated(
    monkeypatch: pytest.MonkeyPatch, name: str, attr: str, message: str | None
) -> None:
    ep = EntryPoint(name=name, value=f"{_FAKE}:{attr}", group=plugins.ENTRY_POINT_GROUP)
    monkeypatch.setattr(plugins, "entry_points", lambda *, group: [ep])
    if message is None:
        assert plugins.discover_plugins()["pay"].graphs[0].name == "invoice"
    else:
        with pytest.raises(plugins.PluginError, match=message):
            plugins.discover_plugins()
