# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Every finding code a review reason can carry (``check:<code>``) has a plain sentence in
English and Arabic in the web app (``Pay.reasonCodes``), so the invoice view never falls
back to the bare code. The codes are read from the checks' ``finding(...)`` calls."""

from __future__ import annotations

import ast
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
CHECKS = ROOT / "packages" / "pay" / "pairly_pay" / "checks"
MESSAGES = ROOT / "apps" / "web" / "messages"


def _literal_codes() -> set[str]:
    codes: set[str] = set()
    for path in CHECKS.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "id", None)
            # finding(check, code, rule, ...) and the checks' helpers (code, rule) or
            # (rule, code) with string literals.
            args = [a.value for a in node.args if isinstance(a, ast.Constant)]
            strings = [a for a in args if isinstance(a, str)]
            if name == "finding" and len(node.args) >= 3:
                code = node.args[1]
                if isinstance(code, ast.Constant) and isinstance(code.value, str):
                    codes.add(code.value)
            elif name == "_exception" and strings:
                codes.add(strings[0])
            elif name in {"_bad", "add"} and len(strings) >= 2:
                codes.add(strings[1])
    return codes


def test_every_finding_code_has_a_sentence_in_both_languages() -> None:
    codes = _literal_codes() | {
        # Codes built from a variable in the checks.
        "policy_violation",
        "prompt_injection",
        "hidden_text",
        "metadata_injection",
        "unusual_unicode",
        "guardrail_other",
        "metadata_inconsistency",
    }
    assert {"bank_mismatch", "near_duplicate", "arithmetic_error", "no_po"} <= codes
    for locale in ("en", "ar"):
        sentences = json.loads((MESSAGES / f"{locale}.json").read_text(encoding="utf-8"))
        known = sentences["Pay"]["reasonCodes"]
        missing = sorted(c for c in codes if c not in known and c != "po_matched")
        assert not missing, (locale, missing)
        assert all(isinstance(v, str) and v.strip() for v in known.values())
