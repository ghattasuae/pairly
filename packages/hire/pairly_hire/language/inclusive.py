# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology

"""Inclusive-language checker for job text, English and Arabic. Deterministic.

Each rule is a pattern with a category, a plain explanation (in the language of the
text) and suggested alternatives. Categories:

* ``gendered``: words that address one gender or are coded as one (``salesman``,
  ``guys``, ``ذكور فقط``, a masculine-only job title in an Arabic ad).
* ``age``: age limits and age-coded words (``young``, ``digital native``,
  ``recent graduate``, ``لا يزيد عمره عن``).
* ``origin``: native speaker or nationality wording that excludes (nationality rules
  some jobs must follow, such as Emiratisation, are flagged for a check, not as wrong).
* ``personal``: asks for things a job ad should not (photo, marital status, religion).
* ``ability``: ableist wording (``crazy``, ``able-bodied``).
* ``jargon``: exclusionary jargon (``rockstar``, ``ninja``, ``culture fit``).

A finding is advice. The model is not needed for any of this; explanations are fixed
text so the same input always gives the same result.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Final

from pairly_hire.language import Finding


@dataclass(frozen=True, slots=True)
class Rule:
    id: str
    lang: str
    category: str
    pattern: re.Pattern[str]
    message: str
    suggestions: tuple[str, ...]
    severity: str = "warning"


def _en(
    rule_id: str,
    category: str,
    pattern: str,
    message: str,
    *suggestions: str,
    severity: str = "warning",
) -> Rule:
    return Rule(
        rule_id,
        "en",
        category,
        re.compile(pattern, re.IGNORECASE),
        message,
        suggestions,
        severity,
    )


def _ar(
    rule_id: str,
    category: str,
    pattern: str,
    message: str,
    *suggestions: str,
    severity: str = "warning",
) -> Rule:
    return Rule(rule_id, "ar", category, re.compile(pattern), message, suggestions, severity)


# Arabic word boundaries: \b does not work well with Arabic prefixes (و, ال, ب), so
# the Arabic patterns use explicit "not an Arabic letter" lookarounds.
# An Arabic word may carry attached prefixes (و, ف, ب, ك, ل, ال and their combinations).
_A = r"(?<![ء-ي])(?:بال|وال|فال|كال|لل|ال|و|ف|ب|ك|ل)?"
_Z = r"(?![ء-ي])"

EN_RULES: Final[tuple[Rule, ...]] = (
    _en(
        "en.gendered.salesman",
        "gendered",
        r"\b(sales|chair|fore|work|business|middle)m[ae]n\b",
        "This job title names one gender.",
        "salesperson",
        "chair",
        "supervisor",
        "worker",
        "businessperson",
    ),
    _en(
        "en.gendered.manpower",
        "gendered",
        r"\bman(power|hours?|kind)\b",
        "This word names one gender.",
        "workforce",
        "staff hours",
        "people",
    ),
    _en(
        "en.gendered.guys",
        "gendered",
        r"\b(guys|gentlemen)\b",
        "Addresses one gender. Use a word for everyone.",
        "everyone",
        "team",
        "people",
    ),
    _en(
        "en.gendered.pronoun",
        "gendered",
        r"\b(he|she) (will|should|must|is expected to|has to)\b",
        "Describes the person in this job as one gender.",
        "you will",
        "they will",
        "the person in this role will",
    ),
    _en(
        "en.gendered.only",
        "gendered",
        r"\b(male|female|men|women)\s+(only|candidates only|preferred|applicants only)\b",
        "Limits the job to one gender.",
        "remove the gender requirement",
    ),
    _en(
        "en.gendered.coded",
        "gendered",
        r"\b(aggressive|dominant|dominate|fearless|competitive spirit|strong-willed)\b",
        "This word is often read as masculine and can put people off applying.",
        "ambitious",
        "driven",
        "confident",
        severity="info",
    ),
    _en(
        "en.age.young",
        "age",
        r"\b(young|youthful|energetic young)\b",
        "Refers to age. Describe the skills the job needs instead.",
        "motivated",
        "enthusiastic",
    ),
    _en(
        "en.age.digital_native",
        "age",
        r"\bdigital natives?\b",
        "Implies an age group.",
        "confident with digital tools",
    ),
    _en(
        "en.age.recent_graduate",
        "age",
        r"\b(recent|new) (graduate|grad)s?\b",
        "Implies an age group. Say what experience level you want.",
        "early career",
        "entry level",
    ),
    _en(
        "en.age.limit",
        "age",
        r"\bage(d|\s+limit)?:?\s+(under|below|over|above|between|of|up to)?\s*\d{2}"
        r"(\s*(and|-|to)\s*\d{2})?\b|"
        r"\b(under|below|over|above|between)\s+\d{2}(\s*(and|-|to)\s*\d{2})?\s*"
        r"(years old|yrs old|years of age)",
        "Sets an age limit. Age limits are discriminatory in most places.",
        "remove the age limit",
    ),
    _en(
        "en.age.max_years",
        "age",
        r"\bno more than \d+ years (of )?experience\b",
        "A maximum on experience can exclude older candidates.",
        "remove the maximum",
        "describe the level of the role",
        severity="info",
    ),
    _en(
        "en.origin.native",
        "origin",
        r"\bnative (english|arabic|french|german|spanish|hindi|urdu)?\s*speakers?\b",
        "Native speaker excludes fluent people by origin.",
        "fluent in",
        "professional working proficiency in",
    ),
    _en(
        "en.origin.nationality",
        "origin",
        r"\b[a-z]+ nationals? only\b|\bnationality:\s*\w+",
        "Limits the job by nationality. Check that a law requires it (for example "
        "Emiratisation) and say so.",
        "state the legal requirement",
        severity="info",
    ),
    _en(
        "en.personal.photo",
        "personal",
        r"\b(attach|include|send|with) (a |your )?(recent )?(photo|photograph|picture)\b",
        "Asking for a photo invites bias. Do not ask for one.",
        "remove the photo request",
    ),
    _en(
        "en.personal.marital",
        "personal",
        r"\b(single|married|unmarried)\s+(candidates?|applicants?|only|preferred)\b|"
        r"\bmarital status\b",
        "Marital status is not a job requirement.",
        "remove it",
    ),
    _en(
        "en.personal.religion",
        "personal",
        r"\breligion\b",
        "Religion is not a job requirement.",
        "remove it",
    ),
    _en(
        "en.ability.crazy",
        "ability",
        r"\b(crazy|insane|lame|dumb)\b",
        "This word can hurt people with disabilities.",
        "very",
        "remarkable",
        "unusual",
    ),
    _en(
        "en.ability.able_bodied",
        "ability",
        r"\b(able-bodied|physically fit)\b",
        "Excludes people with disabilities. Name the actual task if it matters.",
        "able to lift 10 kg with or without adjustments",
    ),
    _en(
        "en.jargon.rockstar",
        "jargon",
        r"\b(rock\s?stars?|gurus?|wizards?|ninjas?|superstars?|unicorns?)\b",
        "Jargon like this puts many qualified people off.",
        "expert",
        "skilled engineer",
        "specialist",
    ),
    _en(
        "en.jargon.culture_fit",
        "jargon",
        r"\bculture fit\b",
        "Culture fit often means hiring people like the current team.",
        "shares our values",
        "adds to our culture",
    ),
    _en(
        "en.jargon.work_hard_play_hard",
        "jargon",
        r"\bwork hard,? play hard\b",
        "Suggests long hours and a narrow social life.",
        "describe working hours and flexibility",
        severity="info",
    ),
)

_AR_TITLES = (
    "مهندس",
    "محاسب",
    "مدير",
    "موظف",
    "مطور",
    "مصمم",
    "مندوب",
    "مسؤول",
    "محلل",
    "مشرف",
    "منسق",
    "أخصائي",
    "مبرمج",
    "سكرتير",
)

AR_RULES: Final[tuple[Rule, ...]] = (
    _ar(
        "ar.gendered.only",
        "gendered",
        _A + r"(ل?ل?ذكور|ل?ل?إناث|ل?ل?رجال|ل?ل?نساء)\s+فقط" + _Z,
        "يحصر الوظيفة في جنس واحد.",
        "احذف شرط الجنس",
    ),
    _ar(
        "ar.gendered.title",
        "gendered",
        r"(?:مطلوب|نبحث عن|يطلب|نوظف)\s+("
        + "|".join(_AR_TITLES)
        # Not followed by a feminine form ("/ة") or "(ذكر/أنثى)".
        + r")(?![ء-ي/])(?!\s*\((?:ذكر|أنثى|ذكور|إناث))",
        "المسمى الوظيفي بصيغة المذكر فقط. أضف صيغة المؤنث ليشعر الجميع أنهم مدعوون.",
        "مهندس/ة",
        "(ذكر/أنثى)",
    ),
    _ar(
        "ar.age.young",
        "age",
        _A + r"(شاب|شابة|شباب)" + _Z,
        "يشير إلى العمر. صف المهارات المطلوبة بدلا من ذلك.",
        "متحمس",
        "لديه دافع",
    ),
    _ar(
        "ar.age.limit",
        "age",
        r"(لا\s+يزيد|لا\s+يتجاوز|لا\s+يقل)\s+(عمره|عمرها|العمر|السن|سنه)|"
        r"(العمر|السن)\s*(من|بين|:)?\s*\d+|"
        r"(أقل|أكثر)\s+من\s+\d+\s+(سنة|عاما)\s+من\s+العمر",
        "يحدد عمرا معينا. حدود العمر تمييز في معظم الأماكن.",
        "احذف شرط العمر",
    ),
    _ar(
        "ar.age.recent_graduate",
        "age",
        _A + r"(حديث|حديثة|حديثي)\s+(ال)?تخرج" + _Z,
        "يشير إلى فئة عمرية. حدد مستوى الخبرة بدلا من ذلك.",
        "في بداية المسيرة المهنية",
        "مستوى مبتدئ",
    ),
    _ar(
        "ar.origin.native",
        "origin",
        r"(متحدث|متحدثة|ناطق|ناطقة)\s+(أصلي|أصلية|أصليين)|اللغة\s+الأم",
        "شرط المتحدث الأصلي يستبعد من يجيد اللغة بسبب أصله.",
        "يجيد",
        "طلاقة في",
    ),
    _ar(
        "ar.origin.nationality",
        "origin",
        r"(للمواطنين|مواطنين|للجنسية\s+\S+)\s+فقط",
        "يحصر الوظيفة في جنسية. تأكد من وجود شرط قانوني (مثل التوطين) واذكره.",
        "اذكر الشرط القانوني",
        severity="info",
    ),
    _ar(
        "ar.personal.photo",
        "personal",
        r"(صورة\s+شخصية|إرفاق\s+صورة|مع\s+صورة)",
        "طلب الصورة يفتح الباب للتحيز. لا تطلبها.",
        "احذف طلب الصورة",
    ),
    _ar(
        "ar.personal.marital",
        "personal",
        _A + r"(أعزب|عزباء|متزوج|متزوجة|الحالة\s+الاجتماعية)" + _Z,
        "الحالة الاجتماعية ليست شرطا للوظيفة.",
        "احذفها",
    ),
    _ar(
        "ar.personal.religion",
        "personal",
        _A + r"(الديانة|الدين)" + _Z,
        "الدين ليس شرطا للوظيفة.",
        "احذفه",
    ),
    _ar(
        "ar.ability.fit",
        "ability",
        r"(لائق|لائقة|لائقا)\s+(بدنيا|طبيا)|سليم\s+البنية",
        "يستبعد الأشخاص ذوي الإعاقة. اذكر المهمة الفعلية إن كانت مهمة.",
        "قادر على أداء المهام مع تسهيلات معقولة",
    ),
    _ar(
        "ar.jargon.rockstar",
        "jargon",
        r"(نينجا|روك\s*ستار|نجم\s+روك|ساحر)",
        "مصطلحات كهذه تبعد كثيرا من المؤهلين.",
        "خبير",
        "متخصص",
    ),
)

RULES: Final = (*EN_RULES, *AR_RULES)


def _normalize(text: str) -> str:
    # NFC keeps offsets stable for ordinary text; we never change the length.
    return unicodedata.normalize("NFC", text)


def check_inclusive(text: str, *, lang: str | None = None) -> list[Finding]:
    """Findings for ``text``, ordered by position. ``lang`` limits the rules to one
    language (``en`` or ``ar``); by default both run, so mixed text is covered."""
    source = _normalize(text)
    findings: list[Finding] = []
    for rule in RULES:
        if lang is not None and rule.lang != lang:
            continue
        for match in rule.pattern.finditer(source):
            if not match.group(0).strip():
                continue
            findings.append(
                Finding(
                    rule=rule.id,
                    category=rule.category,
                    severity=rule.severity,
                    start=match.start(),
                    end=match.end(),
                    match=match.group(0),
                    message=rule.message,
                    suggestions=rule.suggestions,
                )
            )
    findings.sort(key=lambda f: (f.start, f.rule))
    return findings
