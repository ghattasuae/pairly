# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Vendor impersonation signals (brief 4.4.4).

The origin domain is the forwarded original sender's when an AP mailbox forwarded the
invoice, else the sender's (uploads and the API have none, so the sender rules do not
apply to them).

* ``sender_lookalike_domain`` (high): the domain imitates a known vendor domain
  (``pairly_pay.checks.domains``: homoglyphs, IDN, another ending, extra letters, one
  edit).
* ``sender_domain_mismatch`` (medium): the matched vendor does not use this domain.
* ``first_time_sender`` (medium): no earlier invoice came from it and no vendor uses it.
* ``unknown_vendor`` (medium): no vendor in the master matched the invoice.
* ``urgency_language`` (medium): pressure to pay now or to change the account, in
  English or Arabic, anywhere in the document text.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Final

from pairly_core.risk.types import Severity
from pairly_pay.checks.domains import is_own, lookalike
from pairly_pay.checks.messages import term, terms
from pairly_pay.checks.types import CheckResult, Finding, History, InvoiceFacts, finding, result

CHECK: Final = "impersonation"
VERSION: Final = "1"

# (phrase key, pattern). English is matched case-insensitively on words; Arabic after
# removing diacritics and normalising alef and taa marbuta forms.
_I: Final = re.I
URGENCY: Final[tuple[tuple[str, re.Pattern[str]], ...]] = (
    ("pay_today", re.compile(r"\b(pay|paid|payment|settle\w*|transfer)\b[^.\n]{0,40}today", _I)),
    ("pay_today", re.compile(r"\btoday\b[^.\n]{0,20}\b(pay|payment|transfer)\b", _I)),
    ("urgent", re.compile(r"\b(urgent|urgently|asap|right away)\b", _I)),
    ("immediately", re.compile(r"\b(process|pay|transfer|release)\w*[^.\n]{0,30}immediately", _I)),
    ("new_account", re.compile(r"\bnew\s+(bank\s+)?(account|iban|banking)\b", _I)),
    ("new_account", re.compile(r"\b(bank|account)\s+details\s+(have\s+)?changed\b", _I)),
    ("update_records", re.compile(r"\bupdate\s+your\s+(records|files|system)\b", _I)),
    ("suspension", re.compile(r"\b(suspension|suspend\w*|disconnect\w*)[^.\n]{0,30}service", _I)),
    ("suspension", re.compile(r"\bavoid\b[^.\n]{0,20}\b(suspension|penalt\w+|legal action)", _I)),
    ("final_notice", re.compile(r"\bfinal\s+(notice|reminder|demand)\b", _I)),
    ("urgent", re.compile("\u0639\u0627\u062c\u0644")),
    ("pay_today", re.compile(r"(السداد|الدفع|التحويل)[^.\n]{0,20}اليوم")),
    ("immediately", re.compile(r"(فورا|على الفور)")),
    ("new_account", re.compile(r"حساب\s+(مصرفي\s+|بنكي\s+)?جديد")),
    ("update_records", re.compile(r"تحديث\s+(سجلاتكم|السجلات|بياناتكم)")),
    ("suspension", re.compile(r"(ايقاف|قطع)\s+الخدمه")),
)  # fmt: skip
_DIACRITICS: Final = re.compile("[\u064b-\u0652\u0640]")
# Alef forms to bare alef, taa marbuta to haa, alef maqsura to yaa.
_ARABIC_FOLD: Final = str.maketrans(
    {
        "\u0623": "\u0627",
        "\u0625": "\u0627",
        "\u0622": "\u0627",
        "\u0629": "\u0647",
        "\u0649": "\u064a",
    }
)


def _normalise_arabic(text: str) -> str:
    return _DIACRITICS.sub("", unicodedata.normalize("NFKC", text)).translate(_ARABIC_FOLD)


def urgency_phrases(text: str) -> list[str]:
    """The urgency phrase keys found in ``text`` (each once, in pattern order)."""
    if not text:
        return []
    folded = _normalise_arabic(text)
    # PDF producers often write Arabic in visual order: read each line reversed too.
    reversed_lines = "\n".join(line[::-1] for line in folded.split("\n"))
    found: list[str] = []
    for key, pattern in URGENCY:
        if key not in found and (pattern.search(folded) or pattern.search(reversed_lines)):
            found.append(key)
    return found


def check_impersonation(facts: InvoiceFacts, history: History) -> CheckResult:
    findings: list[Finding] = []
    vendor = facts.vendor
    domain = facts.origin_domain.lower() if facts.origin_domain else None
    all_known = [d for _, d in history.vendor_domains]
    if domain:
        known_here = any(is_own(domain, d) for d in all_known)
        imitation = None if known_here else lookalike(domain, all_known)
        if imitation is not None:
            findings.append(
                finding(
                    CHECK,
                    "sender_lookalike_domain",
                    "sender_lookalike_domain",
                    Severity.HIGH,
                    evidence={
                        "domain": domain,
                        "lookalike_of": imitation.known,
                        "how": imitation.how,
                    },
                    domain=domain,
                    lookalike_of=imitation.known,
                    how=term(imitation.how),
                )
            )
        if vendor is not None and not any(is_own(domain, d) for d in vendor.email_domains):
            findings.append(
                finding(
                    CHECK,
                    "sender_domain_mismatch",
                    "sender_domain_mismatch",
                    Severity.MEDIUM,
                    evidence={"domain": domain, "vendor_id": vendor.id},
                    domain=domain,
                )
            )
        if not known_here and domain not in history.seen_domains:
            findings.append(
                finding(
                    CHECK,
                    "first_time_sender",
                    "first_time_sender",
                    Severity.MEDIUM,
                    evidence={"domain": domain},
                    domain=domain,
                )
            )
    if vendor is None:
        findings.append(finding(CHECK, "unknown_vendor", "unknown_vendor", Severity.MEDIUM))
    text = facts.document.text if facts.document is not None else ""
    phrases = urgency_phrases(text)
    if phrases:
        findings.append(
            finding(
                CHECK,
                "urgency_language",
                "urgency_language",
                Severity.MEDIUM,
                evidence={"phrases": ",".join(phrases)},
                phrase=terms(phrases),
            )
        )
    return result(CHECK, VERSION, findings)
