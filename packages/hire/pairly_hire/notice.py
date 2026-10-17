# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""The AI-use notice a candidate reads and accepts before applying (brief 3.2.2).

Plain language: what the AI does, that people make every decision, and how to ask for a
human review or for deletion. Each wording has a version; an application stores the
version the candidate accepted, the time and the language. Changing the wording means a
new version (the old text stays here, so any stored consent can be shown again).

Copy style: short plain sentences, no em dashes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final


@dataclass(frozen=True, slots=True)
class Notice:
    version: str
    title: str
    points: tuple[str, ...]
    consent: str


CURRENT_VERSION: Final = "2026-10-1"

NOTICES: Final[dict[str, dict[str, Notice]]] = {
    "2026-10-1": {
        "en": Notice(
            version="2026-10-1",
            title="How we use AI in this application",
            points=(
                "An AI assistant reads your CV and answers and compares them with the "
                "requirements of this job.",
                "It highlights evidence for each requirement and can flag missing "
                "information. It never makes a decision.",
                "A person on our hiring team reviews every application and makes every decision.",
                "The AI does not see the optional equal opportunity questions. Those "
                "answers are stored apart and used only for anonymous fairness reports.",
                "You can ask for a person to review any AI-assisted step, and you can ask "
                "us to delete your data. Reply to your acknowledgement email or use the "
                "candidate portal.",
                "If we do not hire you, we delete your application after the period in "
                "our privacy policy, unless you agree to stay in our talent pool.",
            ),
            consent="I have read how AI is used and I want to apply.",
        ),
        "ar": Notice(
            version="2026-10-1",
            title="كيف نستخدم الذكاء الاصطناعي في هذا الطلب",
            points=(
                "يقرأ مساعد ذكاء اصطناعي سيرتك الذاتية وإجاباتك ويقارنها بمتطلبات هذه الوظيفة.",
                "يبرز الأدلة لكل متطلب وقد يشير إلى معلومات ناقصة. لا يتخذ أي قرار أبدا.",
                "يراجع شخص من فريق التوظيف كل طلب ويتخذ كل القرارات.",
                "لا يرى الذكاء الاصطناعي أسئلة تكافؤ الفرص الاختيارية. تحفظ هذه الإجابات "
                "منفصلة وتستخدم فقط لتقارير العدالة دون أسماء.",
                "يمكنك أن تطلب مراجعة بشرية لأي خطوة ساعد فيها الذكاء الاصطناعي، ويمكنك "
                "أن تطلب حذف بياناتك. رد على رسالة تأكيد الاستلام أو استخدم بوابة "
                "المرشحين.",
                "إذا لم نوظفك، نحذف طلبك بعد المدة المذكورة في سياسة الخصوصية، إلا إذا "
                "وافقت على البقاء في قائمة المواهب لدينا.",
            ),
            consent="قرأت كيف يستخدم الذكاء الاصطناعي وأرغب في التقديم.",
        ),
    }
}


def current_notice(locale: str) -> Notice:
    texts = NOTICES[CURRENT_VERSION]
    return texts.get(locale, texts["en"])
