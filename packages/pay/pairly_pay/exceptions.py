# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""The exceptions queue (brief 4.5.4): invoices waiting for a person, by category, each
with the explanation the checks already wrote and a suggested resolution, plus drafted
messages to the vendor or the requester.

**Categories** (``CATEGORIES``), from the stored check results and the review reasons:

* matching findings: ``no_po``, ``price_variance``, ``quantity_variance``,
  ``missing_receipt`` (other matching findings: ``other``);
* duplicates: ``duplicate_suspect``; the bank check: ``bank_mismatch``; the policy
  check: ``policy_breach``;
* reading (a doubtful extraction, nothing read, a document that could not be parsed):
  ``low_confidence``;
* anything else (another check, the vendor link, the guardrails, the risk): ``other``.

The explanation is the finding's own sentence (English and Arabic, ``checks.messages``);
nothing is generated. An invoice is overdue after ``exception_sla_hours`` (check
settings) since it was received.

**Drafted messages** (``MESSAGE_TEMPLATES``): fixed English and Arabic paragraphs per
category and recipient ("please send the delivery note", "the PO number is missing").
Never for a bank mismatch: those vendors are called on the number on file, never
emailed. An optional personal paragraph comes from the model (``pay_exception_note/v1``)
and is checked here before a person sees it (``check_note``: no em dash, link, email
address, phone number or number, short). The model sees the template's purpose and the
language only, never the invoice, the vendor or a person, so recordings answer for every
invoice. The person may edit the text; it is sent only after another person approved
exactly that text (``pairly_pay.messages``).
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Annotated, Any, Final

from pydantic import BaseModel, ConfigDict, StringConstraints

from pairly_core.llm.types import CallContext, RouterResult, TaskSpec
from pairly_core.notifications.templating import has_em_dash
from pairly_pay.models import MessageRecipient, PayInvoice

if TYPE_CHECKING:
    from pairly_core.llm.router import ModelRouter

CATEGORIES: Final = (
    "no_po",
    "price_variance",
    "quantity_variance",
    "missing_receipt",
    "duplicate_suspect",
    "bank_mismatch",
    "policy_breach",
    "low_confidence",
    "other",
)
_MATCHING: Final = {
    "no_po": "no_po",
    "price_variance": "price_variance",
    "quantity_variance": "quantity_variance",
    "missing_receipt": "missing_receipt",
}
_BY_CHECK: Final = {
    "duplicates": "duplicate_suspect",
    "bank": "bank_mismatch",
    "policy": "policy_breach",
}
_READING: Final = (
    "extraction",
    "not_read",
    "document_not_parsed",
    "einvoice_unreadable",
)

RESOLUTIONS: Final[dict[str, tuple[str, str]]] = {
    "no_po": (
        "Ask the vendor for the purchase order number, or correct the PO number on the "
        "invoice so it matches the right purchase order.",
        "اطلب من المورد رقم أمر الشراء، أو صحح رقم أمر الشراء في الفاتورة ليطابق أمر الشراء "
        "الصحيح.",
    ),
    "price_variance": (
        "Compare the prices with the purchase order. Ask the vendor for a credit note or a "
        "corrected invoice, or accept the difference with a reason.",
        "قارن الأسعار مع أمر الشراء. اطلب من المورد إشعارا دائنا أو فاتورة مصححة، أو اقبل "
        "الفرق مع ذكر السبب.",
    ),
    "quantity_variance": (
        "Check what was received. Ask the vendor for a credit note, or record the missing "
        "goods receipt if more arrived.",
        "تحقق مما استُلم. اطلب من المورد إشعارا دائنا، أو سجل إيصال الاستلام الناقص إذا "
        "وصلت كمية أكبر.",
    ),
    "missing_receipt": (
        "Record the goods receipt when the goods arrive: the matching runs again by itself. "
        "Ask the vendor or the requester for the delivery note.",
        "سجل إيصال استلام البضائع عند وصولها: تعاد المطابقة تلقائيا. اطلب إشعار التسليم من "
        "المورد أو من صاحب الطلب.",
    ),
    "duplicate_suspect": (
        "Compare it with the suspected original side by side. Reject it if it is a copy; "
        "otherwise mark it reviewed.",
        "قارنها بالفاتورة الأصلية المشتبه بها جنبا إلى جنب. ارفضها إن كانت نسخة، وإلا "
        "فعلّمها كمراجعة.",
    ),
    "bank_mismatch": (
        "Do not reply to the email. Call the vendor on the phone number on file and follow "
        "the bank-detail change ticket.",
        "لا ترد على البريد الإلكتروني. اتصل بالمورد على رقم الهاتف المسجل وتابع طلب تغيير "
        "البيانات البنكية.",
    ),
    "policy_breach": (
        "Read the policy finding and ask the requester for the business reason before the "
        "invoice goes for approval.",
        "اقرأ ملاحظة السياسة واطلب من صاحب الطلب سبب الشراء قبل إرسال الفاتورة للاعتماد.",
    ),
    "low_confidence": (
        "Check the highlighted fields against the document and correct them, or ask the "
        "vendor for a clear copy.",
        "قارن الحقول المحددة بالمستند وصححها، أو اطلب من المورد نسخة واضحة.",
    ),
    "other": (
        "Open the invoice and read the findings: correct a field, link the vendor, or reject it.",
        "افتح الفاتورة واقرأ الملاحظات: صحح حقلا، أو اربط المورد، أو ارفضها.",
    ),
}
# Reasons without a finding sentence of their own (review reasons, not check findings).
REASON_TEXT: Final[dict[str, tuple[str, str]]] = {
    "extraction": (
        "Some fields were read with low confidence and need a person.",
        "قُرئت بعض الحقول بثقة منخفضة وتحتاج إلى شخص.",
    ),
    "not_read": ("Nothing could be read from the document.", "تعذرت قراءة أي شيء من المستند."),
    "document_not_parsed": ("The document could not be opened.", "تعذر فتح المستند."),
    "einvoice_unreadable": (
        "The electronic invoice could not be read.",
        "تعذرت قراءة الفاتورة الإلكترونية.",
    ),
    "vendor": ("The vendor needs a person to confirm it.", "يحتاج المورد إلى تأكيد من شخص."),
    "guardrails": (
        "The document safety checks found something to look at.",
        "وجدت فحوص سلامة المستند ما يستدعي النظر.",
    ),
    "risk": ("The risk score needs a person.", "درجة الخطر تحتاج إلى شخص."),
    "check_error": ("A check could not run.", "تعذر تشغيل أحد الفحوص."),
}


# ------------------------------------------------------------------ categorising


@dataclass(frozen=True, slots=True)
class Explanation:
    en: str
    ar: str
    check: str | None = None
    rule: str | None = None


@dataclass
class Category:
    key: str
    explanations: list[Explanation]

    @property
    def resolution(self) -> tuple[str, str]:
        return RESOLUTIONS[self.key]

    @property
    def templates(self) -> list[MessageTemplate]:
        return [t for t in MESSAGE_TEMPLATES.values() if t.category == self.key]


def _finding_category(check: str, code: str) -> str:
    if check == "matching":
        return _MATCHING.get(code, "other")
    return _BY_CHECK.get(check, "other")


def _reason_kind(reason: str) -> str:
    kind = reason.split(":", 1)[0]
    if kind.startswith("vendor"):
        return "vendor"
    if kind.startswith("guardrails"):
        return "guardrails"
    if kind in {"check_error", "check_failed"}:
        return "check_error"
    return kind


def categorise(invoice: PayInvoice) -> list[Category]:
    """The categories of an invoice waiting for a person, in ``CATEGORIES`` order, each
    with the sentences that explain it. Never empty for an invoice that needs a person."""
    found: dict[str, list[Explanation]] = {}

    def add(key: str, explanation: Explanation | None) -> None:
        items = found.setdefault(key, [])
        if explanation is not None and explanation not in items and len(items) < 10:
            items.append(explanation)

    for item in invoice.checks or []:
        if not isinstance(item, Mapping):
            continue
        check = str(item.get("check", ""))
        if item.get("status") == "error":
            en, ar = REASON_TEXT["check_error"]
            add("other", Explanation(en, ar, check))
            continue
        for f in item.get("findings") or []:
            if not isinstance(f, Mapping) or not f.get("needs_review"):
                continue
            raw = f.get("message")
            message: Mapping[str, Any] = raw if isinstance(raw, Mapping) else {}
            add(
                _finding_category(check, str(f.get("code", ""))),
                Explanation(
                    str(message.get("en", "")),
                    str(message.get("ar", "")),
                    check,
                    str(f.get("rule", "")) or None,
                ),
            )
    for reason in invoice.review_reasons or []:
        kind = _reason_kind(str(reason))
        if kind in {"check", "risk"} and found:
            continue  # explained by the findings above
        text = REASON_TEXT.get(kind)
        key = "low_confidence" if kind in _READING else "other"
        if kind == "check":
            key, text = "other", None
        add(key, Explanation(*text) if text else None)
    if not found:
        found["other"] = []
    return [Category(k, found[k]) for k in CATEGORIES if k in found]


@dataclass(frozen=True, slots=True)
class Age:
    hours: int
    overdue: bool


def age_of(invoice: PayInvoice, sla_hours: int, now: datetime | None = None) -> Age:
    now = now or datetime.now(UTC)
    hours = max(0, int((now - invoice.created_at).total_seconds() // 3600))
    return Age(hours, hours >= sla_hours)


# ------------------------------------------------------------------ message templates


@dataclass(frozen=True, slots=True)
class MessageTemplate:
    id: str
    category: str
    recipient: MessageRecipient
    purpose: str
    en: str
    ar: str


def _t(
    id_: str, category: str, recipient: MessageRecipient, purpose: str, en: str, ar: str
) -> MessageTemplate:
    return MessageTemplate(id_, category, recipient, purpose, en, ar)


V, R = MessageRecipient.VENDOR, MessageRecipient.REQUESTER
MESSAGE_TEMPLATES: Final[dict[str, MessageTemplate]] = {
    t.id: t
    for t in (
        _t(
            "po_missing", "no_po", V, "Ask the vendor for the missing purchase order number.",
            "Thank you for invoice {invoice_number}. It does not show a purchase order number, "
            "so we cannot process it yet. Please send us the purchase order number for this "
            "invoice, or a corrected invoice that shows it.",
            "شكرا لكم على الفاتورة {invoice_number}. لا تتضمن الفاتورة رقم أمر شراء، لذا لا "
            "يمكننا معالجتها بعد. يرجى إرسال رقم أمر الشراء الخاص بهذه الفاتورة، أو فاتورة "
            "مصححة تتضمنه.",
        ),
        _t(
            "delivery_note", "missing_receipt", V, "Ask the vendor for the delivery note.",
            "Thank you for invoice {invoice_number} for purchase order {po}. We have not "
            "recorded the delivery of these goods yet. Please send the delivery note or proof "
            "of delivery so we can match the invoice.",
            "شكرا لكم على الفاتورة {invoice_number} الخاصة بأمر الشراء {po}. لم نسجل استلام "
            "هذه البضائع بعد. يرجى إرسال إشعار التسليم أو ما يثبت التسليم حتى نتمكن من مطابقة "
            "الفاتورة.",
        ),
        _t(
            "record_receipt", "missing_receipt", R,
            "Ask the requester to record the goods receipt.",
            "Invoice {invoice_number} for purchase order {po} is waiting for a goods receipt. "
            "If the goods have arrived, please record the receipt in Pairly so the invoice can "
            "be matched.",
            "الفاتورة {invoice_number} الخاصة بأمر الشراء {po} بانتظار إيصال استلام البضائع. "
            "إذا وصلت البضائع، يرجى تسجيل الاستلام في Pairly حتى تتم مطابقة الفاتورة.",
        ),
        _t(
            "price_query", "price_variance", V,
            "Ask the vendor to confirm prices that are above the purchase order.",
            "Thank you for invoice {invoice_number}. Some unit prices are higher than on "
            "purchase order {po}. Please confirm the prices, or send a credit note or a "
            "corrected invoice at the agreed prices.",
            "شكرا لكم على الفاتورة {invoice_number}. بعض أسعار الوحدات أعلى مما في أمر الشراء "
            "{po}. يرجى تأكيد الأسعار، أو إرسال إشعار دائن أو فاتورة مصححة بالأسعار المتفق "
            "عليها.",
        ),
        _t(
            "quantity_query", "quantity_variance", V,
            "Ask the vendor about quantities above what was ordered or received.",
            "Thank you for invoice {invoice_number}. Some quantities are more than we ordered "
            "or received on purchase order {po}. Please send a credit note or a corrected "
            "invoice for the quantities delivered.",
            "شكرا لكم على الفاتورة {invoice_number}. بعض الكميات أكبر مما طلبناه أو استلمناه "
            "في أمر الشراء {po}. يرجى إرسال إشعار دائن أو فاتورة مصححة بالكميات المسلمة.",
        ),
        _t(
            "confirm_delivery", "quantity_variance", R,
            "Ask the requester to check what was delivered.",
            "Invoice {invoice_number} for purchase order {po} bills more than was received. "
            "Please check what was delivered and record any missing goods receipt in Pairly.",
            "الفاتورة {invoice_number} الخاصة بأمر الشراء {po} تطالب بأكثر مما استُلم. يرجى "
            "التحقق مما سُلّم وتسجيل أي إيصال استلام ناقص في Pairly.",
        ),
        _t(
            "duplicate_query", "duplicate_suspect", V,
            "Ask the vendor whether an invoice is new or a copy.",
            "We received invoice {invoice_number}, which looks like an invoice we already "
            "have. Please confirm whether it is a new invoice or a copy of an earlier one.",
            "استلمنا الفاتورة {invoice_number}، وتبدو مثل فاتورة لدينا بالفعل. يرجى تأكيد ما "
            "إذا كانت فاتورة جديدة أو نسخة من فاتورة سابقة.",
        ),
        _t(
            "policy_query", "policy_breach", R,
            "Ask the requester for the business reason of a purchase outside the policy.",
            "Invoice {invoice_number} from {vendor} does not meet our spend policy as it "
            "stands. Please confirm the business reason for this purchase so it can be "
            "reviewed.",
            "الفاتورة {invoice_number} من {vendor} لا تتوافق مع سياسة الإنفاق في وضعها "
            "الحالي. يرجى تأكيد سبب هذا الشراء حتى تتم مراجعتها.",
        ),
        _t(
            "clear_copy", "low_confidence", V, "Ask the vendor for a clear copy of an invoice.",
            "Thank you for invoice {invoice_number}. Some details on it could not be read "
            "clearly. Please send a clear PDF copy of the invoice.",
            "شكرا لكم على الفاتورة {invoice_number}. تعذرت قراءة بعض تفاصيلها بوضوح. يرجى "
            "إرسال نسخة واضحة من الفاتورة بصيغة PDF.",
        ),
    )
}  # fmt: skip
_PLACEHOLDER: Final = re.compile(r"\{([a-z_]{1,30})\}")


def fill(template: MessageTemplate, locale: str, invoice: PayInvoice, po_number: str | None) -> str:
    """The template paragraph for this invoice (plain substitution, never ``format``)."""
    values = {
        "invoice_number": invoice.invoice_number or invoice.reference,
        "po": po_number or invoice.po_number or ("غير مذكور" if locale == "ar" else "not given"),
        "vendor": invoice.vendor_name or "",
    }
    text = template.ar if locale == "ar" else template.en
    return _PLACEHOLDER.sub(lambda m: str(values.get(m.group(1), ""))[:120], text)


# ------------------------------------------------------------------ the personal paragraph

NOTE_TASK: Final = TaskSpec(name="pay_exception_note", prompt_version="v1")
MAX_NOTE: Final = 300
MAX_MESSAGE: Final = 3000
_Note = Annotated[str, StringConstraints(strip_whitespace=True, max_length=MAX_NOTE)]
_URL: Final = re.compile(r"(https?://|www\.)\S+", re.IGNORECASE)
_EMAIL: Final = re.compile(r"[^\s@]+@[^\s@]+\.[A-Za-z]{2,}")
_DIGIT: Final = re.compile(r"[0-9\u0660-\u0669]")
_LATIN: Final = re.compile(r"[A-Za-z]")


class ExceptionNote(BaseModel):
    """What the model returns: one short personal paragraph."""

    model_config = ConfigDict(extra="forbid")

    note: _Note


def note_inputs(template: MessageTemplate, locale: str) -> dict[str, Any]:
    """What the model sees: the purpose and the language. No invoice, vendor or person."""
    return {
        "template": template.id,
        "recipient": template.recipient.value,
        "purpose": template.purpose,
        "language": "ar" if locale == "ar" else "en",
    }


def check_note(note: str, locale: str) -> list[str]:
    """Every reason to refuse the paragraph (empty: it may be shown)."""
    problems: list[str] = []
    if not note.strip():
        problems.append("empty")
    if len(note) > MAX_NOTE:
        problems.append("too_long")
    if has_em_dash(note):
        problems.append("em_dash")
    if _URL.search(note):
        problems.append("link")
    if _EMAIL.search(note):
        problems.append("email_address")
    if _DIGIT.search(note):
        problems.append("number")
    if locale == "ar" and _LATIN.search(note):
        problems.append("latin_letters")
    return problems


def check_message(text: str) -> list[str]:
    """What a person may send: some text, not too long, no em dash, no control characters."""
    problems: list[str] = []
    if not text.strip():
        problems.append("empty")
    if len(text) > MAX_MESSAGE:
        problems.append("too_long")
    if has_em_dash(text):
        problems.append("em_dash")
    if re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", text):
        problems.append("control_characters")
    return problems


@dataclass(frozen=True, slots=True)
class AiNote:
    note: str | None
    problems: tuple[str, ...]
    call_id: uuid.UUID | None = None
    from_recording: bool = False


async def ai_note(
    router: ModelRouter,
    org_id: uuid.UUID,
    template: MessageTemplate,
    locale: str,
    *,
    invoice_id: uuid.UUID,
    actor_id: uuid.UUID | None,
) -> AiNote:
    """Ask the model for the personal paragraph. Raises ``ModelUnavailable`` (fail
    closed: the template alone is shown); a paragraph that fails ``check_note`` is
    dropped with its problems."""
    result: RouterResult[ExceptionNote] = await router.complete(
        task=NOTE_TASK,
        inputs=note_inputs(template, locale),
        schema=ExceptionNote,
        org_id=org_id,
        context=CallContext(
            actor_id=str(actor_id) if actor_id else None,
            workflow="pay",
            entity_type="pay.invoice",
            entity_id=str(invoice_id),
        ),
    )
    problems = check_note(result.value.note, locale)
    if problems:
        return AiNote(None, tuple(problems), result.call_id, result.from_recording)
    return AiNote(result.value.note, (), result.call_id, result.from_recording)


def compose(base: str, note: str | None) -> str:
    return base if not note else f"{base}\n\n{note}"


def template_for(category: str, template_id: str) -> MessageTemplate | None:
    found = MESSAGE_TEMPLATES.get(template_id)
    return found if found is not None and found.category == category else None


def templates_of(categories: Sequence[Category]) -> list[MessageTemplate]:
    return [t for c in categories for t in c.templates]
