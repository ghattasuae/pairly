# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
# ruff: noqa: E501  (one sentence per line reads better than split strings)
"""Reviewer explanations for guardrail findings, in English and Arabic.

Each finding maps to one key, ``guardrails.<type>.<code>``, with one plain sentence per
locale. ``{subject}`` is "This CV", "This invoice" and so on; ``{evidence}`` is a short,
cleaned excerpt of what was found (control characters removed, cut at 80 characters).
The web app can take the same keys and strings into its message catalogue.

The evidence comes from an untrusted document: the UI must render it as text, never
as HTML.
"""

from __future__ import annotations

from typing import Literal

from pairly_core.guardrails.textnorm import strip_invisible

Locale = Literal["en", "ar"]
DocKind = Literal["document", "cv", "invoice", "email"]

SUBJECTS: dict[str, dict[str, str]] = {
    "en": {
        "document": "This document",
        "cv": "This CV",
        "invoice": "This invoice",
        "email": "This email",
    },
    "ar": {
        "document": "هذا المستند",
        "cv": "هذه السيرة الذاتية",
        "invoice": "هذه الفاتورة",
        "email": "هذا البريد الإلكتروني",
    },
}

# en: "{subject} ...". ar: "في {subject} ..." works for both grammatical genders.
MESSAGES: dict[str, dict[str, str]] = {
    "guardrails.hidden_text.low_contrast": {
        "en": '{subject} contains white or near-background text that a person cannot see: "{evidence}".',
        "ar": 'في {subject} نص أبيض أو بلون قريب من الخلفية لا يراه القارئ: "{evidence}".',
    },
    "guardrails.hidden_text.background_colour": {
        "en": '{subject} contains text set in the page background colour, so a person cannot see it: "{evidence}".',
        "ar": 'في {subject} نص بلون خلفية الصفحة نفسه، لذلك لا يراه القارئ: "{evidence}".',
    },
    "guardrails.hidden_text.invisible_render_mode": {
        "en": '{subject} contains invisible text that only software can read: "{evidence}".',
        "ar": 'في {subject} نص غير مرئي لا تقرؤه إلا البرامج: "{evidence}".',
    },
    "guardrails.hidden_text.tiny_font": {
        "en": '{subject} contains text in a font too small for a person to read: "{evidence}".',
        "ar": 'في {subject} نص بخط صغير جدا لا يمكن للقارئ رؤيته: "{evidence}".',
    },
    "guardrails.hidden_text.off_page": {
        "en": '{subject} contains text placed outside the visible page: "{evidence}".',
        "ar": 'في {subject} نص موضوع خارج حدود الصفحة المرئية: "{evidence}".',
    },
    "guardrails.hidden_text.covered_or_clipped": {
        "en": '{subject} contains text that is covered by a shape or cut off, so a person cannot see it: "{evidence}".',
        "ar": 'في {subject} نص مغطى بشكل مرسوم فوقه أو مقصوص، لذلك لا يراه القارئ: "{evidence}".',
    },
    "guardrails.hidden_text.docx_hidden": {
        "en": '{subject} contains text marked as hidden in Word, so a person does not see it: "{evidence}".',
        "ar": 'في {subject} نص مخفي بإعدادات Word، لذلك لا يراه القارئ: "{evidence}".',
    },
    "guardrails.instruction.ignore_instructions": {
        "en": '{subject} contains text that tells an AI system to ignore its instructions: "{evidence}".',
        "ar": 'في {subject} نص يطلب من نظام الذكاء الاصطناعي تجاهل تعليماته: "{evidence}".',
    },
    "guardrails.instruction.role_override": {
        "en": '{subject} contains text that tries to change the role of an AI system: "{evidence}".',
        "ar": 'في {subject} نص يحاول تغيير دور نظام الذكاء الاصطناعي: "{evidence}".',
    },
    "guardrails.instruction.system_prompt": {
        "en": '{subject} mentions the hidden instructions of an AI system: "{evidence}".',
        "ar": 'في {subject} إشارة إلى التعليمات الداخلية لنظام الذكاء الاصطناعي: "{evidence}".',
    },
    "guardrails.instruction.ranking_manipulation": {
        "en": '{subject} contains text that tells an AI system how to rank or score it: "{evidence}".',
        "ar": 'في {subject} نص يطلب من نظام الذكاء الاصطناعي ترتيبه أو تقييمه بطريقة معينة: "{evidence}".',
    },
    "guardrails.instruction.approval_manipulation": {
        "en": '{subject} contains text that tells an AI system to approve or mark it as paid: "{evidence}".',
        "ar": 'في {subject} نص يطلب من نظام الذكاء الاصطناعي اعتماده أو اعتباره مدفوعا: "{evidence}".',
    },
    "guardrails.instruction.bank_change": {
        "en": '{subject} contains text that asks to change bank details: "{evidence}".',
        "ar": 'في {subject} نص يطلب تغيير البيانات البنكية: "{evidence}".',
    },
    "guardrails.instruction.review_bypass": {
        "en": '{subject} contains text that asks to skip a check or a review: "{evidence}".',
        "ar": 'في {subject} نص يطلب تجاوز فحص أو مراجعة: "{evidence}".',
    },
    "guardrails.instruction.model_addressing": {
        "en": '{subject} contains text written to an AI system rather than to a person: "{evidence}".',
        "ar": 'في {subject} نص موجه إلى نظام ذكاء اصطناعي لا إلى شخص: "{evidence}".',
    },
    "guardrails.instruction.role_marker": {
        "en": '{subject} contains chat role markers used to inject instructions: "{evidence}".',
        "ar": 'في {subject} علامات أدوار محادثة تستخدم لحقن التعليمات: "{evidence}".',
    },
    "guardrails.instruction.tool_directive": {
        "en": '{subject} contains a command or JSON aimed at an AI tool: "{evidence}".',
        "ar": 'في {subject} أمر أو JSON موجه إلى أداة ذكاء اصطناعي: "{evidence}".',
    },
    "guardrails.instruction.output_control": {
        "en": '{subject} contains text that tells an AI system what to answer: "{evidence}".',
        "ar": 'في {subject} نص يحدد لنظام الذكاء الاصطناعي ما يجب أن يجيب به: "{evidence}".',
    },
    "guardrails.instruction.classifier_paraphrase": {
        "en": '{subject} contains reworded text that a classifier found aimed at an AI system or an automated reviewer: "{evidence}".',
        "ar": 'في {subject} نص معاد صياغته وجده المصنف موجها إلى نظام ذكاء اصطناعي أو مراجع آلي: "{evidence}".',
    },
    "guardrails.instruction.classifier_unavailable": {
        "en": '{subject} was not fully checked for reworded instructions, because the classifier did not complete: "{evidence}".',
        "ar": 'لم يكتمل فحص {subject} بحثا عن تعليمات معاد صياغتها لأن المصنف لم يكتمل: "{evidence}".',
    },
    "guardrails.instruction.hidden_message": {
        "en": '{subject} contains a message hidden in invisible characters: "{evidence}".',
        "ar": 'في {subject} رسالة مخفية في رموز غير مرئية: "{evidence}".',
    },
    "guardrails.keyword_stuffing.repetition": {
        "en": "{subject} repeats the same keyword far more often than normal writing does: {evidence}.",
        "ar": "في {subject} تكرار لنفس الكلمة المفتاحية أكثر بكثير من الكتابة العادية: {evidence}.",
    },
    "guardrails.keyword_stuffing.skill_list": {
        "en": '{subject} contains a long list of keywords with no sentences around them: "{evidence}".',
        "ar": 'في {subject} قائمة طويلة من الكلمات المفتاحية دون جمل توضحها: "{evidence}".',
    },
    "guardrails.metadata_injection.metadata_instruction": {
        "en": '{subject} hides instructions in its file properties: "{evidence}".',
        "ar": 'في {subject} تعليمات مخفية في خصائص الملف: "{evidence}".',
    },
    "guardrails.metadata_injection.metadata_text": {
        "en": '{subject} keeps a long text in its file properties, where a person does not see it: "{evidence}".',
        "ar": 'في {subject} نص طويل في خصائص الملف حيث لا يراه القارئ: "{evidence}".',
    },
    "guardrails.metadata_injection.filename_instruction": {
        "en": '{subject} has a file name that contains instructions: "{evidence}".',
        "ar": 'اسم ملف {subject} يحتوي على تعليمات: "{evidence}".',
    },
    "guardrails.metadata_injection.email_instruction": {
        "en": '{subject} arrived in an email whose subject or headers contain instructions: "{evidence}".',
        "ar": 'في عنوان البريد الإلكتروني المرسل مع هذا الملف أو في ترويساته تعليمات: "{evidence}".',
    },
    "guardrails.unusual_unicode.bidi_override": {
        "en": '{subject} uses characters that reverse the reading direction to disguise text: "{evidence}".',
        "ar": 'في {subject} رموز تعكس اتجاه القراءة لإخفاء النص الحقيقي: "{evidence}".',
    },
    "guardrails.unusual_unicode.bidi_control": {
        "en": '{subject} contains text direction control characters: "{evidence}".',
        "ar": 'في {subject} رموز للتحكم في اتجاه النص: "{evidence}".',
    },
    "guardrails.unusual_unicode.zero_width": {
        "en": '{subject} contains invisible zero-width characters inside words: "{evidence}".',
        "ar": 'في {subject} رموز غير مرئية بعرض صفري داخل الكلمات: "{evidence}".',
    },
    "guardrails.unusual_unicode.tag_characters": {
        "en": '{subject} contains invisible tag characters that can carry a hidden message: "{evidence}".',
        "ar": 'في {subject} رموز وسوم غير مرئية قد تحمل رسالة مخفية: "{evidence}".',
    },
    "guardrails.unusual_unicode.variation_selectors": {
        "en": '{subject} contains a long run of invisible variation selectors: "{evidence}".',
        "ar": 'في {subject} سلسلة طويلة من محددات التنويع غير المرئية: "{evidence}".',
    },
    "guardrails.unusual_unicode.homoglyph": {
        "en": '{subject} contains words that mix look-alike letters from different alphabets: "{evidence}".',
        "ar": 'في {subject} كلمات تخلط بين حروف متشابهة الشكل من أبجديات مختلفة: "{evidence}".',
    },
}

FALLBACK = {
    "en": '{subject} contains content that may try to manipulate an AI system: "{evidence}".',
    "ar": 'في {subject} محتوى قد يحاول التلاعب بنظام الذكاء الاصطناعي: "{evidence}".',
}

MAX_EVIDENCE_CHARS = 80


def clean_evidence(evidence: str, limit: int = MAX_EVIDENCE_CHARS) -> str:
    text = " ".join(strip_invisible(evidence).replace('"', "'").split())
    if len(text) > limit:
        text = text[: limit - 3].rstrip() + "..."
    return text


def explain(
    finding_type: str,
    code: str,
    evidence: str,
    locale: Locale = "en",
    doc_kind: DocKind = "document",
) -> str:
    key = f"guardrails.{finding_type}.{code}"
    lang = locale if locale in SUBJECTS else "en"
    template = MESSAGES.get(key, FALLBACK)[lang]
    subject = SUBJECTS[lang].get(doc_kind, SUBJECTS[lang]["document"])
    return template.format(subject=subject, evidence=clean_evidence(evidence))
