# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Prompt-injection findings (brief 4.4.7): the guardrail report of the invoice's
document, as Pay findings.

The guardrails (``pairly_core.guardrails``) already ran in the graph, and the text
they flagged for removal never reached the model (``sanitize``). This check makes the
findings part of the invoice's checks and risk score, and the reviewer sees them (the
evidence text itself is shown only to people who process invoices, by the API).

Mapping: an instruction to the model is ``prompt_injection``, hidden text
``hidden_text``, an instruction in metadata ``metadata_injection`` (and
``prompt_injection``), unusual characters ``unusual_unicode``. A guardrail ``high`` is
critical here, ``medium`` high, ``low`` low (the engine's ``core.guardrail_findings``
mapping). No report, or a guardrail detector that failed, is an ``error``.
"""

from __future__ import annotations

from typing import Final

from pairly_core.guardrails.report import FindingType
from pairly_core.guardrails.report import Severity as GuardrailSeverity
from pairly_core.risk.types import Severity
from pairly_pay.checks.types import CheckResult, Finding, InvoiceFacts, error, finding, result

CHECK: Final = "injection"
VERSION: Final = "1"
_SEVERITY: Final = {
    GuardrailSeverity.LOW: Severity.LOW,
    GuardrailSeverity.MEDIUM: Severity.HIGH,
    GuardrailSeverity.HIGH: Severity.CRITICAL,
}
_RULE: Final = {
    FindingType.INSTRUCTION: ("prompt_injection", "prompt_injection"),
    FindingType.HIDDEN_TEXT: ("hidden_text", "hidden_text"),
    FindingType.METADATA_INJECTION: ("metadata_injection", "metadata_injection"),
    FindingType.UNUSUAL_UNICODE: ("unusual_unicode", "unusual_unicode"),
}


def check_injection(facts: InvoiceFacts) -> CheckResult:
    report = facts.guardrail_report
    if report is None:
        return error(CHECK, VERSION, "guardrail_report_missing")
    if report.errors:
        return error(CHECK, VERSION, "guardrail_detector_failed")
    findings: list[Finding] = []
    for item in report.findings:
        code, rule = _RULE.get(item.type, ("guardrail_other", "guardrail_other"))
        findings.append(
            finding(
                CHECK,
                code,
                rule,
                _SEVERITY.get(item.severity, Severity.CRITICAL),
                evidence={
                    "guardrail_finding": item.id[:16],
                    "type": item.type.value,
                    "code": item.code[:64],
                    "location": item.location[:64],
                    "page": item.page,
                    "removed": item.remove,
                },
                detail=item.code[:64],
            )
        )
    return result(CHECK, VERSION, findings)
