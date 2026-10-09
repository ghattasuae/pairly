# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Patterns for model-directed instructions, in English and Arabic.

Patterns run on ``fold_for_matching`` output: lower case, invisible characters
removed, Cyrillic and Greek look-alikes mapped to Latin, Arabic diacritics removed and
letter variants unified (so Arabic patterns are written with bare alef, ya for alef
maqsura and ha for ta marbuta).

They target text addressed to a model or an automated reviewer, not ordinary
business language: "please pay this invoice within 30 days" is fine, "approve this
invoice" and "pay it without review" are not; "I ignored the noise" is fine, "ignore
the previous instructions" is not.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from pairly_core.guardrails.report import Severity


@dataclass(frozen=True, slots=True)
class InstructionPattern:
    code: str
    severity: Severity
    regex: re.Pattern[str]
    lang: str


def _p(code: str, severity: Severity, pattern: str, lang: str = "en") -> InstructionPattern:
    return InstructionPattern(code, severity, re.compile(pattern, re.MULTILINE), lang)


H, M, L = Severity.HIGH, Severity.MEDIUM, Severity.LOW

# Up to three filler words between key terms.
_GAP = r"(?:[\w'-]+\s+){0,3}?"
_AI = (
    r"(?:ai|a\.i\.|llm|gpt|chatgpt|claude|gemini|copilot|language\s+model|model|assistant|"
    r"bot|chatbot|screener|screening\s+(?:ai|system|tool|bot)|ats|automated\s+"
    r"(?:system|reviewer|screener|parser|agent))"
)

EN: tuple[InstructionPattern, ...] = (
    _p(
        "ignore_instructions",
        H,
        r"\b(?:ignore|disregard|forget|override|overrule|discard)\s+"
        r"(?:(?:all|any|the|your|my|these|those|every|of|previous|prior|above|earlier|"
        r"preceding|former|original|system|existing|other|given)\s+){0,4}"
        r"(?:instructions?|prompts?|rules|directions|directives|guidelines|context|"
        r"guidance|constraints|rubric|criteria|scoring\s+rules|policies|policy)\b",
    ),
    _p(
        "ignore_instructions",
        H,
        r"\b(?:ignore|disregard|forget)\s+(?:everything|all|anything)\s+"
        r"(?:above|before|previously|you\s+(?:were|have\s+been)\s+told)\b",
    ),
    _p("role_override", H, r"\byou\s+are\s+now\s+(?:a|an|the|in|acting|my|no\s+longer)\b"),
    _p("role_override", H, r"\bfrom\s+now\s+on,?\s+(?:you|act|respond|answer)\b"),
    _p(
        "role_override",
        H,
        r"\b(?:act|behave|respond)\s+as\s+(?:if\s+you\s+(?:are|were)\s+)?(?:an?\s+|the\s+)?"
        r"(?:unrestricted|jailbroken|different|new|helpful\s+hr|admin|administrator|"
        r"approver|recruiter|hiring\s+manager|finance\s+controller|dan)\b",
    ),
    _p("role_override", H, r"\bpretend\s+(?:to\s+be|you\s+are|that\s+you)\b"),
    _p("role_override", M, r"\bnew\s+(?:instructions|role|persona|task)\s*:"),
    _p(
        "system_prompt",
        H,
        r"\b(?:reveal|print|show|repeat|output|leak|ignore|override|replace)\s+"
        r"(?:the\s+|your\s+)?(?:system|developer|hidden|initial)\s+"
        r"(?:prompt|message|instructions?)\b",
    ),
    _p("system_prompt", L, r"\b(?:system|developer)\s+(?:prompt|message)\b"),
    _p("system_prompt", M, r"\b(?:jailbreak|dan\s+mode|developer\s+mode\s+enabled)\b"),
    _p(
        "ranking_manipulation",
        H,
        r"\b(?:rank|rate|score|place|put|move|list|mark|consider|classify|grade)\s+"
        r"(?:(?:this|the|my)\s+(?:candidate|applicant|cv|resume|profile|application|"
        r"submission)|me|him|her|them)\s+(?:as\s+)?(?:the\s+)?"
        r"(?:first|top|highest|number\s+one|#\s*1|first\s+place|at\s+the\s+top|best|"
        r"excellent|a\s+perfect\s+(?:match|fit)|strongly\s+qualified)\b",
    ),
    _p(
        "ranking_manipulation",
        H,
        r"\b(?:give|assign|award|grant)\s+(?:this\s+|the\s+)?(?:candidate|applicant|cv|"
        r"resume|me|him|her|them)?\s*(?:a\s+|the\s+)?(?:perfect|maximum|max|top|highest|"
        r"full|10\s*/\s*10|100\s*/\s*100|5\s*/\s*5)\s*(?:score|rating|marks|points|grade)",
    ),
    _p(
        "ranking_manipulation",
        H,
        r"\balways\s+(?:shortlist|hire|select|advance|recommend|approve)\s+(?:this|me|him|her|"
        r"them|the\s+(?:candidate|applicant|invoice|payment)\b(?!s))",
    ),
    _p(
        "ranking_manipulation",
        M,
        r"\byou\s+(?:must|should|need\s+to|have\s+to)\s+(?:shortlist|hire|select|advance|"
        r"recommend|approve)\b",
    ),
    _p(
        "ranking_manipulation",
        M,
        r"\b(?:recommend|shortlist|hire|select|advance)\s+(?:this|the)\s+"
        r"(?:candidate|applicant)\s+(?:immediately|now|without|for\s+the\s+(?:role|job))\b",
    ),
    _p(
        "approval_manipulation",
        H,
        r"\b(?:approve|authori[sz]e|auto-?approve)\s+this\s+"
        r"(?:invoice|payment|bill|transfer|request|claim|expense)\b",
    ),
    _p(
        "approval_manipulation",
        M,
        r"\b(?:approve|authori[sz]e|auto-?approve)\s+(?:the|my|our)\s+"
        r"(?:invoice|payment|bill|transfer)\s+(?:immediately|now|today|automatically)\b",
    ),
    _p(
        "approval_manipulation",
        H,
        r"\b(?:pay|process|release|settle)\s+(?:this|the|it)\s*(?:invoice|payment|bill)?\s+"
        r"(?:immediately|automatically|right\s+away|without\s+(?:review|approval|"
        r"verification|checks?|delay))\b",
    ),
    _p(
        "approval_manipulation",
        H,
        r"\bmark\s+(?:this\s+|the\s+|it\s+)?(?:invoice\s+|bill\s+|payment\s+)?as\s+"
        r"(?:paid|approved|verified|legitimate|safe)\b",
    ),
    _p(
        "approval_manipulation",
        H,
        r"\b(?:invoice|payment)\s+(?:is|has\s+been)\s+(?:pre-?)?approved\s+"
        r"(?:by\s+(?:the\s+)?(?:cfo|ceo|finance|management))",
    ),
    _p(
        "bank_change",
        H,
        r"\b(?:change|update|replace|switch|modify|amend)\s+(?:the\s+|our\s+|your\s+|"
        r"vendor'?s?\s+|supplier'?s?\s+|payee\s+)*(?:bank(?:ing)?\s+(?:details|account|"
        r"information|info)|iban|account\s+(?:number|details)|beneficiary\s+"
        r"(?:details|account))\b",
    ),
    _p(
        "review_bypass",
        H,
        r"\b(?:skip|bypass|override|disable|ignore|turn\s+off)\s+(?:the\s+|all\s+|any\s+)?"
        r"(?:(?:approval|review|verification|validation)\b|(?:fraud|duplicate|compliance|"
        r"human|manual)\s+(?:checks?|review|detection|approval|step)\b)",
    ),
    _p(
        "review_bypass",
        M,
        r"\bdo\s+not\s+(?:flag|report|mention|escalate|verify|check|question|review)\b",
    ),
    _p(
        "model_addressing",
        H,
        r"\bif\s+you\s+are\s+(?:an?\s+)?(?:ai|llm|language\s+model|assistant|bot|"
        r"automated|machine|gpt|chatgpt|claude)\b",
    ),
    _p(
        "model_addressing",
        M,
        r"\b(?:dear|attention|note\s+to|message\s+to|instructions?\s+(?:for|to)|hey|hi|"
        r"hello)\s*,?\s+(?:the\s+|any\s+)?" + _AI + r"\b",
    ),
    _p(
        "model_addressing",
        M,
        (
            r"\b(?:ai|llm|gpt|chatgpt|claude)\s*[:,]\s*(?:please\s+)?"
            r"(?:ignore|rank|rate|score|approve|mark|give|say|output|respond)\b"
        ),
    ),
    _p("model_addressing", L, r"\bas\s+an?\s+(?:ai|language\s+model)\b"),
    _p(
        "role_marker",
        M,
        r"^[ \t>#*-]*(?:system|assistant|developer|human|user)\s*:\s*(?:you\b|ignore\b|"
        r"from\s+now|new\s+instructions|the\s+(?:candidate|applicant|invoice|user)\b)",
    ),
    _p(
        "role_marker",
        M,
        r"<\|?(?:im_start|im_end|system|endoftext)\|?>|\[/?inst\]|<<\s*/?sys\s*>>|"
        r"</?(?:system|instructions?|assistant|user)\s*>|###\s*(?:instruction|system|"
        r"response)\b",
    ),
    _p(
        "tool_directive",
        M,
        r"\{\s*\"(?:tool|tool_name|function|function_call|tool_call|tool_use|action|"
        r"arguments|command)\"\s*:",
    ),
    _p("tool_directive", M, r"<\s*/?\s*(?:tool_call|function_call|tool_use|invoke)\b"),
    _p(
        "tool_directive",
        M,
        r"\b(?:call|invoke)\s+(?:the\s+)?(?:tool|function)\s+[\"'`][a-z_]+",
    ),
    _p(
        "tool_directive",
        H,
        r"\"(?:decision|recommendation|status|score|verdict|risk|label)\"\s*:\s*\"?"
        r"(?:approve|approved|hire|shortlist|paid|pass|low|safe|100|10)\b",
    ),
    _p(
        "output_control",
        M,
        r"\b(?:respond|reply|answer|output|return|say)\s+only\s+(?:with\s+)?",
    ),
    _p(
        "output_control",
        M,
        r"\byour\s+(?:final\s+)?(?:answer|output|response|verdict|decision|score|rating)\s+"
        r"(?:must|should|will)\s+be\s*(?:[:\"']|yes\b|approved?\b|hire\b|shortlist\b|10\b|100\b)",
    ),
)

# Arabic, written in folded form (see module docstring).
_AGAP = r"(?:\S+\s+){0,3}?"
AR: tuple[InstructionPattern, ...] = (
    _p(
        "ignore_instructions",
        H,
        r"(?<!\w)(?:تجاهل|اهمل|انس|انسي|تخط|الغ)\s+" + _AGAP + r"(?:التعليمات|الاوامر|التوجيهات|"
        r"القواعد|المعايير|الارشادات|ما\s+سبق|كل\s+ما\s+سبق)",
        "ar",
    ),
    _p("role_override", H, r"(?:^|\s)انت\s+الان\s+", "ar"),
    _p(
        "role_override",
        M,
        r"(?<!\w)(?:تصرف|تظاهر)\s+(?:ك|بصفتك\s+|بانك\s+|انك\s+)(?:مساعد|نموذج|مسؤول|مدير|مراجع)",
        "ar",
    ),
    _p("system_prompt", M, r"(?<!\w)موجه\s+النظام", "ar"),
    _p("system_prompt", L, r"(?<!\w)(?:تعليمات|رساله|اوامر)\s+النظام", "ar"),
    _p(
        "ranking_manipulation",
        H,
        r"(?<!\w)(?:رتب|ضع|اجعل|صنف|قيم)\s+"
        + _AGAP
        + r"(?:المرشح|المتقدم|السيره)\s+"
        + _AGAP
        + r"(?:الاول|اولا|في\s+المرتبه\s+الاولي|في\s+المقدمه|الافضل|في\s+الصداره)",
        "ar",
    ),
    _p(
        "ranking_manipulation",
        H,
        r"(?<!\w)(?:اعط|امنح|اعطي)\s+"
        + _AGAP
        + r"(?:اعلي|اقصي|كامل|العلامه\s+الكامله|الدرجه\s+الكامله)",
        "ar",
    ),
    _p(
        "approval_manipulation",
        H,
        r"(?<!\w)(?:وافق|اعتمد|صادق|اقبل)\s+(?:علي\s+)?(?:هذه\s+|هذا\s+)?(?:الفاتوره|الدفعه|الدفع|"
        r"التحويل|الطلب)",
        "ar",
    ),
    # "Consider / mark / record this invoice as paid": an imperative aimed at the
    # invoice itself. A CV line about a payments ledger ("سجل مدفوعات") is not one.
    _p(
        "approval_manipulation",
        H,
        r"(?<!\w)(?:اعتبر|ضع\s+علامه(?:\s+علي)?|سجل)\s+(?:هذه\s+|هذا\s+)?"
        r"(?:الفاتوره|الدفعه|المبلغ|الطلب)\s+(?:ك|علي\s+انها\s+|بانها\s+|انها\s+)?"
        r"(?:مدفوعه|مدفوع|معتمده|معتمد)(?!\w)",
        "ar",
    ),
    _p("approval_manipulation", H, r"(?<!\w)اعتبرها\s+(?:ك)?(?:مدفوعه|معتمده)(?!\w)", "ar"),
    # A request to change bank details: an imperative ("حدث بيانات البنك"), or a verbal
    # noun after a request word ("يرجى تحديث ...", "قم بتغيير ..."). A bare verbal noun
    # is a statement ("تحققت من كل تغيير في البيانات البنكية"), not a request.
    _p(
        "bank_change",
        H,
        r"(?:(?<!\w)(?:غير|حدث|استبدل|عدل|بدل)\s+|(?<!\w)(?:يرجي|الرجاء|نرجو|نرجوا|يجب|"
        r"ينبغي|يلزم)\s+(?:\S+\s+)?(?:تغيير|تحديث|تعديل|استبدال)\s+|(?<!\w)قم\s+ب(?:تغيير|"
        r"تحديث|تعديل|استبدال)\s+)(?:\S+\s+){0,2}?(?:الحساب\s+البنكي|بيانات\s+البنك|"
        r"تفاصيل\s+البنك|البيانات\s+البنكيه|رقم\s+الايبان|الايبان|رقم\s+الحساب|تفاصيل\s+الحساب)",
        "ar",
    ),
    _p(
        "review_bypass",
        H,
        r"(?<!\w)(?:تجاوز|تخط|عطل)\s+" + _AGAP + r"(?:المراجعه|التحقق|الموافقه|الفحص|التدقيق)",
        "ar",
    ),
    _p(
        "review_bypass",
        H,
        r"(?<!\w)(?:ادفع|اعتمد|حول|نفذ|سدد)\s+" + _AGAP + r"(?:بدون|دون|من\s+غير)\s+(?:اي\s+)?"
        r"(?:مراجعه|تحقق|موافقه)",
        "ar",
    ),
    _p(
        "model_addressing",
        M,
        r"(?<!\w)(?:الي|عزيزي|ملاحظه\s+الي|تنبيه\s+الي)\s+(?:نظام\s+)?(?:الذكاء\s+الاصطناعي|"
        r"النموذج|الروبوت)",
        "ar",
    ),
    _p("model_addressing", H, r"اذا\s+كنت\s+(?:نموذج|ذكاء\s+اصطناعي|مساعد|روبوت)", "ar"),
)

PATTERNS: tuple[InstructionPattern, ...] = EN + AR
