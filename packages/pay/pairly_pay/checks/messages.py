# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""One-sentence explanations of every finding, in English and Arabic.

Templates use ``{name}`` placeholders filled by plain substitution (never
``str.format``). A value may be a pair ``(en, ar)`` when it has words in it; numbers,
ids and domains are the same in both languages (wrapped in direction isolates in
Arabic so they read correctly). A missing value shows as ``?``.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, Final

_PLACEHOLDER: Final = re.compile(r"\{([a-z_]{1,40})\}")
# First strong isolate and pop directional isolate: an id or number inside Arabic text.
_FSI: Final = "\u2068"  # first strong isolate, as an escape (no literal bidi controls)
_PDI: Final = "\u2069"  # pop directional isolate, as an escape

MESSAGES: Final[dict[str, tuple[str, str]]] = {
    # ---------------------------------------------------------------- arithmetic
    "line_total_mismatch": (
        "Line {line}: {quantity} x {unit_price} is {expected}, but the line total says {printed}.",
        "البند {line}: حاصل {quantity} في {unit_price} هو {expected}، لكن إجمالي البند "
        "المطبوع {printed}.",
    ),
    "subtotal_mismatch": (
        "The subtotal {printed} is not the sum of the lines, which is {expected}.",
        "المجموع الفرعي {printed} لا يساوي مجموع البنود، وهو {expected}.",
    ),
    "line_vat_mismatch": (
        "Line {line}: VAT at {rate}% of {base} is {expected}, but the line says {printed}.",
        "البند {line}: ضريبة {rate}% على {base} هي {expected}، لكن البند يذكر {printed}.",
    ),
    "vat_sum_mismatch": (
        "The VAT total {printed} is not the sum of the line VAT, which is {expected}.",
        "إجمالي الضريبة {printed} لا يساوي مجموع ضريبة البنود، وهو {expected}.",
    ),
    "vat_rate_mismatch": (
        "The VAT total {printed} does not match the VAT rates of the lines, which give {expected}.",
        "إجمالي الضريبة {printed} لا يتفق مع نسب الضريبة على البنود، والتي تعطي {expected}.",
    ),
    "rate_not_allowed": (
        "Line {line} charges VAT at {rate}%, which is not a configured tax rate.",
        "البند {line} يحتسب الضريبة بنسبة {rate}%، وهي ليست نسبة ضريبية معتمدة.",
    ),
    "header_rate_not_allowed": (
        "The invoice states a VAT rate of {rate}%, which is not a configured tax rate.",
        "تذكر الفاتورة نسبة ضريبة {rate}%، وهي ليست نسبة ضريبية معتمدة.",
    ),
    "zero_rated_charged": (
        "Line {line} looks like a zero-rated supply but is charged VAT at {rate}%.",
        "البند {line} يبدو توريداً خاضعاً لنسبة الصفر لكن احتسبت عليه ضريبة بنسبة {rate}%.",
    ),
    "zero_rate_unexplained": (
        "A UAE VAT-registered vendor charges no VAT, and nothing on the invoice says the "
        "supply is zero-rated or under reverse charge.",
        "مورد مسجل في ضريبة القيمة المضافة في الإمارات لم يحتسب أي ضريبة، ولا شيء في الفاتورة "
        "يبين أن التوريد بنسبة الصفر أو خاضع للاحتساب العكسي.",
    ),
    "total_mismatch": (
        "The total {printed} is not the subtotal plus VAT, which is {expected}.",
        "الإجمالي {printed} لا يساوي المجموع الفرعي مضافاً إليه الضريبة، وهو {expected}.",
    ),
    # ---------------------------------------------------------------- duplicates
    "same_number": (
        "Invoice number {number} was already received from this vendor on {original}.",
        "رقم الفاتورة {number} سبق استلامه من هذا المورد في {original}.",
    ),
    "near_number": (
        "Invoice number {number} looks like {original_number} from this vendor ({how}), "
        "with the same {same}.",
        "رقم الفاتورة {number} يشبه الرقم {original_number} من هذا المورد ({how})، مع تطابق "
        "{same}.",
    ),
    "same_amount_window": (
        "The same total {amount} {currency} came from this vendor {days} days apart, on "
        "{original}.",
        "ورد الإجمالي نفسه {amount} {currency} من هذا المورد بفارق {days} يوماً، في {original}.",
    ),
    "same_lines": (
        "The same line items were already billed by this vendor on invoice {original_number}.",
        "البنود نفسها سبق أن فوترها هذا المورد في الفاتورة {original_number}.",
    ),
    "same_file": (
        "This exact file was already received on {original}.",
        "هذا الملف نفسه سبق استلامه في {original}.",
    ),
    "same_file_other": (
        "This exact file is also in another document of this organisation.",
        "هذا الملف نفسه موجود أيضاً في مستند آخر لدى هذه المؤسسة.",
    ),
    # ---------------------------------------------------------------- bank
    "bank_mismatch": (
        "The bank account on the invoice (ending {printed}) differs from the vendor master "
        "(ending {on_file}), so payments to this vendor are held until the change is "
        "verified.",
        "الحساب المصرفي في الفاتورة (المنتهي بـ {printed}) يختلف عن سجل المورد (المنتهي بـ "
        "{on_file})، لذلك توقفت المدفوعات لهذا المورد حتى يتم التحقق من التغيير.",
    ),
    "bank_not_on_file": (
        "The invoice has bank details (ending {printed}) but the vendor has none on file, so "
        "payments are held until they are verified.",
        "في الفاتورة بيانات مصرفية (تنتهي بـ {printed}) لكن لا توجد بيانات مسجلة للمورد، لذلك "
        "توقفت المدفوعات حتى يتم التحقق منها.",
    ),
    "bank_unverified": (
        "The invoice has bank details (ending {printed}) but no vendor on file to compare "
        "them with.",
        "في الفاتورة بيانات مصرفية (تنتهي بـ {printed}) لكن لا يوجد مورد مسجل لمقارنتها به.",
    ),
    "bank_iban_invalid": (
        "The IBAN printed on the invoice is not valid ({reason}).",
        "رقم الآيبان المطبوع في الفاتورة غير صحيح ({reason}).",
    ),
    "bank_ticket_failed": (
        "No bank-detail change ticket could be opened ({reason}), so the vendor's payments "
        "are held for a person.",
        "تعذر فتح طلب تغيير البيانات المصرفية ({reason})، لذلك أوقفت مدفوعات المورد إلى أن "
        "يراجعها شخص.",
    ),
    # ---------------------------------------------------------------- impersonation
    "sender_domain_mismatch": (
        "The invoice came from {domain}, which is not one of this vendor's known domains.",
        "وصلت الفاتورة من {domain}، وهو ليس من النطاقات المعروفة لهذا المورد.",
    ),
    "sender_lookalike_domain": (
        "The sender domain {domain} looks like {lookalike_of}, a known vendor domain ({how}).",
        "نطاق المرسل {domain} يشبه {lookalike_of}، وهو نطاق مورد معروف ({how}).",
    ),
    "first_time_sender": (
        "No invoice has come from {domain} before and no vendor uses it.",
        "لم تصل أي فاتورة من {domain} من قبل ولا يستخدمه أي مورد.",
    ),
    "unknown_vendor": (
        "The vendor on this invoice is not in the vendor master.",
        "المورد في هذه الفاتورة غير موجود في سجل الموردين.",
    ),
    "urgency_language": (
        "The invoice presses for fast payment or a change of account ({phrase}).",
        "تضغط الفاتورة للدفع السريع أو لتغيير الحساب ({phrase}).",
    ),
    # ---------------------------------------------------------------- amounts
    "amount_outlier": (
        "The total {amount} {currency} is far from this vendor's usual {median}, over "
        "{count} earlier invoices.",
        "الإجمالي {amount} {currency} بعيد عن المعتاد لهذا المورد وهو {median}، استناداً إلى "
        "{count} فاتورة سابقة.",
    ),
    "round_amount": (
        "The amount {amount} is a round number, which is unusual for an itemised invoice.",
        "المبلغ {amount} رقم مقرّب، وهذا غير معتاد في فاتورة مفصلة البنود.",
    ),
    "split_under_threshold": (
        "This vendor sent {count} invoices within {days} days, each under the {threshold} "
        "{currency} approval limit but {sum} together.",
        "أرسل هذا المورد {count} فواتير خلال {days} أيام، كل منها أقل من حد الموافقة "
        "{threshold} {currency} لكن مجموعها {sum}.",
    ),
    # ---------------------------------------------------------------- forgery
    "template_mismatch": (
        "This invoice does not look like this vendor's earlier ones ({features}).",
        "هذه الفاتورة لا تشبه الفواتير السابقة لهذا المورد ({features}).",
    ),
    "metadata_created_after_modified": (
        "The file says it was created after it was last modified.",
        "يذكر الملف أنه أنشئ بعد آخر تعديل عليه.",
    ),
    "metadata_generic_tool": (
        "The file was made with {tool}, a generic editing or generating tool.",
        "أنشئ الملف باستخدام {tool}، وهي أداة عامة للتحرير أو التوليد.",
    ),
    "metadata_created_late": (
        "The file was created {days} days after the invoice date.",
        "أنشئ الملف بعد {days} يوماً من تاريخ الفاتورة.",
    ),
    "metadata_title_number": (
        "The file title names another invoice number than the one printed.",
        "عنوان الملف يذكر رقم فاتورة غير الرقم المطبوع.",
    ),
    "metadata_author_generic": (
        "The file's author is a generic account name ({author}).",
        "مؤلف الملف اسم حساب عام ({author}).",
    ),
    "editing_overprint": (
        "Some text is printed on top of other text, a sign the document was edited.",
        "بعض النصوص مطبوعة فوق نصوص أخرى، وهذه علامة على تعديل المستند.",
    ),
    "editing_whiteout": (
        "Some figures sit under a white box, a sign a value was painted over.",
        "بعض الأرقام تقع تحت مربع أبيض، وهذه علامة على طمس قيمة.",
    ),
    "trn_invalid": (
        "The printed TRN {trn} is not a valid UAE TRN of 15 digits.",
        "رقم التسجيل الضريبي المطبوع {trn} ليس رقماً إماراتياً صحيحاً من 15 خانة.",
    ),
    "trn_mismatch": (
        "The printed TRN differs from the TRN in the vendor master.",
        "رقم التسجيل الضريبي المطبوع يختلف عن الرقم المسجل في سجل الموردين.",
    ),
    # ---------------------------------------------------------------- injection
    "prompt_injection": (
        "The document contains text that tries to instruct an AI ({detail}); it was flagged "
        "and not followed.",
        "يحتوي المستند على نص يحاول توجيه الذكاء الاصطناعي ({detail})، وقد تم تمييزه ولم يُتّبع.",
    ),
    "hidden_text": (
        "The document contains text a reader cannot see ({detail}).",
        "يحتوي المستند على نص لا يراه القارئ ({detail}).",
    ),
    "metadata_injection": (
        "The file's metadata contains text that tries to instruct an AI.",
        "تحتوي البيانات الوصفية للملف على نص يحاول توجيه الذكاء الاصطناعي.",
    ),
    "unusual_unicode": (
        "The document contains invisible or direction-changing characters ({detail}).",
        "يحتوي المستند على أحرف غير مرئية أو تغير اتجاه النص ({detail}).",
    ),
    "guardrail_other": (
        "The document check found something to look at ({detail}).",
        "وجد فحص المستند أمراً يستحق المراجعة ({detail}).",
    ),
    # ---------------------------------------------------------------- policy
    "policy_rule": ("{reason}", "{reason}"),
    "policy_violated": (
        "The spend policy has a requirement this invoice does not meet: {requirement}.",
        "في سياسة الإنفاق متطلب لا تستوفيه هذه الفاتورة: {requirement}.",
    ),
    "policy_needs_review": (
        "A spend policy requirement needs a person to check it: {requirement}.",
        "يحتاج متطلب في سياسة الإنفاق إلى تحقق شخص: {requirement}.",
    ),
    "policy_review": (
        "The spend policy check needs a person to look ({reason}).",
        "يحتاج فحص سياسة الإنفاق إلى مراجعة شخص ({reason}).",
    ),
    "no_policy_found": (
        "No spend policy covers this invoice, which is not the same as passing it.",
        "لا توجد سياسة إنفاق تغطي هذه الفاتورة، وهذا لا يعني أنها اجتازت الفحص.",
    ),
    # ---------------------------------------------------------------- risk
    "risk_summary": (
        "Risk is {level}: {count} signals, the strongest from the {check} check.",
        "مستوى الخطر {level}: {count} إشارات، أقواها من فحص {check}.",
    ),
    "risk_clean": (
        "Risk is low: no check found anything.",
        "مستوى الخطر منخفض: لم يجد أي فحص شيئاً.",
    ),
    # ---------------------------------------------------------------- matching
    "po_matched": (
        "Matched to purchase order {po}, {kind}: {lines} lines within the tolerances.",
        "طوبقت مع أمر الشراء {po}، مطابقة {kind}: {lines} بنود ضمن الحدود المسموح بها.",
    ),
    "po_not_cited": (
        "The invoice cites no purchase order, so it is not matched and a person approves it.",
        "لا تذكر الفاتورة أي أمر شراء، لذا لم تُطابق ويعتمدها شخص.",
    ),
    "po_required": (
        "The invoice cites no purchase order, but {reason}.",
        "لا تذكر الفاتورة أي أمر شراء، لكن {reason}.",
    ),
    "po_not_on_file": (
        "The invoice cites purchase order {po}, but no purchase orders are kept in Pairly "
        "yet, so it is not matched and a person approves it.",
        "تذكر الفاتورة أمر الشراء {po}، لكن لا تُحفظ أوامر الشراء في Pairly بعد، لذا لم "
        "تُطابق ويعتمدها شخص.",
    ),
    "po_unknown": (
        "The invoice cites purchase order {po}, which is not on file.",
        "تذكر الفاتورة أمر الشراء {po}، وهو غير مسجل.",
    ),
    "po_closed": (
        "Purchase order {po} is {state}: it takes no more invoices.",
        "أمر الشراء {po} {state}: لا يقبل فواتير أخرى.",
    ),
    "po_vendor": (
        "Purchase order {po} belongs to another vendor.",
        "أمر الشراء {po} يخص موردًا آخر.",
    ),
    "po_vendor_unverified": (
        "No vendor is linked, so purchase order {po} cannot be confirmed as this vendor's.",
        "لم يُربط أي مورد، لذا لا يمكن التأكد أن أمر الشراء {po} يخص هذا المورد.",
    ),
    "po_currency": (
        "The invoice is in {currency} but purchase order {po} is in {po_currency}.",
        "الفاتورة بعملة {currency} لكن أمر الشراء {po} بعملة {po_currency}.",
    ),
    "lines_missing": (
        "No line items were read, so the invoice cannot be matched to purchase order {po}.",
        "لم تُقرأ أي بنود، لذا لا يمكن مطابقة الفاتورة مع أمر الشراء {po}.",
    ),
    "line_unreadable": (
        "Line {line} has no quantity or unit price, so it cannot be matched.",
        "البند {line} بلا كمية أو سعر وحدة، لذا لا يمكن مطابقته.",
    ),
    "line_not_on_po": (
        "Line {line} is not on purchase order {po}.",
        "البند {line} غير موجود في أمر الشراء {po}.",
    ),
    "price_over_po": (
        "Line {line}: unit price {price} is {over}% above the PO price {po_price} "
        "(tolerance {tolerance}%).",
        "البند {line}: سعر الوحدة {price} أعلى من سعر أمر الشراء {po_price} بنسبة {over}% "
        "(الحد المسموح {tolerance}%).",
    ),
    "receipt_missing": (
        "Purchase order {po} needs goods received (3-way), and no goods receipt is on file.",
        "أمر الشراء {po} يتطلب استلام البضائع (مطابقة ثلاثية)، ولا يوجد إيصال استلام مسجل.",
    ),
    "line_not_received": (
        "Line {line}: nothing of PO line {po_line} has been received.",
        "البند {line}: لم يُستلم شيء من بند أمر الشراء {po_line}.",
    ),
    "qty_over_received": (
        "Line {line}: {quantity} invoiced, but only {left} of the {received} received is left "
        "to invoice (tolerance {tolerance}%).",
        "البند {line}: فُوتر {quantity}، لكن المتبقي للفوترة {left} فقط من {received} مستلمة "
        "(الحد المسموح {tolerance}%).",
    ),
    "qty_over_ordered": (
        "Line {line}: {quantity} invoiced, but only {left} of the {ordered} ordered is left "
        "to invoice (tolerance {tolerance}%).",
        "البند {line}: فُوتر {quantity}، لكن المتبقي للفوترة {left} فقط من {ordered} مطلوبة "
        "(الحد المسموح {tolerance}%).",
    ),
    "total_over_po": (
        "The subtotal {subtotal} is more than the purchase order value of these lines, {po_value}.",
        "المجموع الفرعي {subtotal} أكبر من قيمة هذه البنود في أمر الشراء، وهي {po_value}.",
    ),
}

# Words inside messages, per language (check names, levels, features, phrases).
TERMS: Final[dict[str, tuple[str, str]]] = {
    "arithmetic": ("arithmetic", "الحساب"),
    "duplicates": ("duplicates", "التكرار"),
    "bank": ("bank details", "البيانات المصرفية"),
    "impersonation": ("sender", "المرسل"),
    "amounts": ("amounts", "المبالغ"),
    "forgery": ("document", "المستند"),
    "injection": ("hidden instructions", "التعليمات المخفية"),
    "policy": ("policy", "السياسة"),
    "matching": ("purchase order matching", "المطابقة مع أمر الشراء"),
    "low": ("low", "منخفض"),
    "medium": ("medium", "متوسط"),
    "high": ("high", "مرتفع"),
    "critical": ("critical", "حرج"),
    "total": ("total", "الإجمالي"),
    "date": ("date", "التاريخ"),
    "lines": ("lines", "البنود"),
    "transposed_digits": ("two digits swapped", "تبديل رقمين"),
    "prefix_or_suffix": ("a prefix or suffix changed", "تغيير بادئة أو لاحقة"),
    "zero_changed": ("a zero added or removed", "إضافة صفر أو حذفه"),
    "separators": ("separators changed", "تغيير الفواصل"),
    "homoglyph": ("lookalike letters", "أحرف متشابهة"),
    "tld_swap": ("another ending", "امتداد مختلف"),
    "extra_text": ("extra letters", "أحرف زائدة"),
    "edit_distance": ("one letter changed", "تغيير حرف"),
    "idn": ("foreign script letters", "أحرف من أبجدية أخرى"),
    "fonts": ("fonts", "الخطوط"),
    "accents": ("colours", "الألوان"),
    "numbering": ("numbering", "الترقيم"),
    "creator": ("billing software", "برنامج الفوترة"),
    "images": ("logo", "الشعار"),
    "page_size": ("page size", "مقاس الصفحة"),
    "pay_today": ("pay today", "الدفع اليوم"),
    "urgent": ("urgent", "عاجل"),
    "immediately": ("immediately", "فوراً"),
    "new_account": ("new bank account", "حساب مصرفي جديد"),
    "update_records": ("update your records", "تحديث السجلات"),
    "suspension": ("service suspension", "إيقاف الخدمة"),
    "final_notice": ("final notice", "إنذار نهائي"),
}


def term(key: str) -> tuple[str, str]:
    return TERMS.get(key, (key, key))


def terms(keys: list[str]) -> tuple[str, str]:
    pairs = [term(k) for k in keys]
    return ", ".join(p[0] for p in pairs), "، ".join(p[1] for p in pairs)


def _fill(template: str, values: Mapping[str, Any], index: int) -> str:
    def repl(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in values or values[name] is None:
            return "?"
        value = values[name]
        if isinstance(value, tuple) and len(value) == 2:
            return str(value[index])
        text = str(value)[:120]
        return f"{_FSI}{text}{_PDI}" if index == 1 else text

    return _PLACEHOLDER.sub(repl, template)


def explain(rule: str, values: Mapping[str, Any]) -> tuple[str, str]:
    """The English and Arabic sentence of ``rule`` filled with ``values``."""
    en, ar = MESSAGES[rule]
    return _fill(en, values, 0), _fill(ar, values, 1)
