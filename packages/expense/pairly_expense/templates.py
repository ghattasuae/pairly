# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
# ruff: noqa: RUF001  (Arabic copy: letters ruff mistakes for Latin look-alikes)
"""System templates of the expense workflow, English and Arabic.

``expense.note.decided`` tells the submitter what happened to their note. Migration
``expense_0001`` stores these as version 1 shared rows (``org_id`` NULL), the way core seeds its
own defaults; an organisation customises them by saving its own version.

Copy style: short plain sentences, never an em dash.
"""

from __future__ import annotations

from typing import Any, Final

from pairly_core.notifications.defaults import DefaultTemplate

DECIDED: Final = "expense.note.decided"
CATEGORY: Final = "updates"


def _v(kind: str, sample: Any, *, required: bool = True) -> dict[str, Any]:
    return {"type": kind, "required": required, "sample": sample}


VARIABLES: Final = {
    "title": _v("string", "Al Noor Cafe, 86.50 AED"),
    "outcome": _v("string", "approved"),
    "reason": _v("string", "Within policy", required=False),
    "note_url": _v("url", "https://app.pairly.example/expense", required=False),
}

_EN_OUTCOME = (
    "{% if outcome == 'approved' %}was approved and is marked for reimbursement"
    "{% elif outcome == 'rejected' %}was rejected"
    "{% else %}needs changes before it can be approved{% endif %}"
)
_AR_OUTCOME = (
    "{% if outcome == 'approved' %}تمت الموافقة عليها ووُضعت علامة لصرفها"
    "{% elif outcome == 'rejected' %}تم رفضها"
    "{% else %}تحتاج إلى تعديلات قبل الموافقة عليها{% endif %}"
)

DECIDED_TEMPLATES: Final = (
    DefaultTemplate(
        key=DECIDED,
        channel="email",
        locale="en",
        category=CATEGORY,
        subject="Expense note: {{ title }}",
        body=(
            "Your expense note {{ title }} " + _EN_OUTCOME + ".\n"
            "{% if reason %}\nReason: {{ reason }}\n{% endif %}"
            "{% if note_url %}\nDetails: {{ note_url }}{% endif %}"
        ),
        variables=VARIABLES,
    ),
    DefaultTemplate(
        key=DECIDED,
        channel="email",
        locale="ar",
        category=CATEGORY,
        subject="مذكرة مصروفات: {{ title }}",
        body=(
            "مذكرة المصروفات {{ title }} " + _AR_OUTCOME + ".\n"
            "{% if reason %}\nالسبب: {{ reason }}\n{% endif %}"
            "{% if note_url %}\nالتفاصيل: {{ note_url }}{% endif %}"
        ),
        variables=VARIABLES,
    ),
    DefaultTemplate(
        key=DECIDED,
        channel="in_app",
        locale="en",
        category=CATEGORY,
        body="Your expense note {{ title }} " + _EN_OUTCOME + ".",
        variables=VARIABLES,
    ),
    DefaultTemplate(
        key=DECIDED,
        channel="in_app",
        locale="ar",
        category=CATEGORY,
        body="مذكرة المصروفات {{ title }} " + _AR_OUTCOME + ".",
        variables=VARIABLES,
    ),
)
