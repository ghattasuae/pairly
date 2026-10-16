# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
# ruff: noqa: E501  (Arabic copy; long scripted demo text)
"""Demo jobs: synthetic briefs and the scripted model answers for them.

The ``hire-demo`` seed opens these jobs, and ``pairly_hire.recordings`` turns each brief
and each drafted description into a recorded model response, so the drafting buttons
work offline in ``recorded`` mode on exactly these inputs. Everything here is invented
(brief hard rule 2). Copy style: plain short sentences, never an em dash.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from pairly_hire.drafting import JobDescriptionDraft
from pairly_hire.rubric import Criterion, KnockoutQuestion, Rubric


@dataclass(frozen=True, slots=True)
class DemoJob:
    slug: str
    brief: str
    team: str
    location: str
    country: str
    work_model: str
    employment_type: str
    salary_min: Decimal
    salary_max: Decimal
    salary_currency: str
    salary_period: str
    show_salary: bool
    draft: JobDescriptionDraft
    rubric: Rubric


def _description(about: str, do: list[str], need: list[str], nice: list[str]) -> str:
    lines = ["About the role", about, "", "What you will do"]
    lines += [f"- {item}" for item in do]
    lines += ["", "What you need"]
    lines += [f"- {item}" for item in need]
    if nice:
        lines += ["", "Nice to have"]
        lines += [f"- {item}" for item in nice]
    return "\n".join(lines)


def _description_ar(about: str, do: list[str], need: list[str], nice: list[str]) -> str:
    lines = ["عن الوظيفة", about, "", "ماذا ستفعل"]
    lines += [f"- {item}" for item in do]
    lines += ["", "ما تحتاجه"]
    lines += [f"- {item}" for item in need]
    if nice:
        lines += ["", "ميزة إضافية"]
        lines += [f"- {item}" for item in nice]
    return "\n".join(lines)


# ------------------------------------------------------------------ backend engineer

_BE_ABOUT = (
    "You will build and run the services that plan deliveries for our customers across "
    "the UAE. You join a team of six engineers who own their services from design to "
    "production."
)
_BE_DO = [
    "Design, build and test Python services and APIs.",
    "Run services on Kubernetes and keep them healthy in production.",
    "Review code and help teammates grow.",
    "Work with product and operations to plan changes.",
]
_BE_NEED = [
    "5 years of experience building backend services in Python.",
    "Experience running services on Kubernetes in production.",
    "Good knowledge of PostgreSQL and SQL.",
    "Clear written English for design documents and reviews.",
]
_BE_NICE = ["Experience with event streaming such as Kafka.", "Spoken or written Arabic."]

BACKEND: Final = DemoJob(
    slug="senior-backend-engineer",
    brief=(
        "Senior backend engineer for the delivery planning team in Dubai, hybrid.\n"
        "Must have 5 years of Python backend work, Kubernetes in production and PostgreSQL.\n"
        "Kafka and Arabic are a plus. Salary 30000 to 38000 AED a month, shown."
    ),
    team="Delivery planning",
    location="Dubai",
    country="AE",
    work_model="hybrid",
    employment_type="full_time",
    salary_min=Decimal("30000"),
    salary_max=Decimal("38000"),
    salary_currency="AED",
    salary_period="month",
    show_salary=True,
    draft=JobDescriptionDraft(
        title="Senior Backend Engineer",
        title_ar="مهندس/ة خلفيات أول",
        summary=(
            "You will build the Python services that plan our deliveries. You will run "
            "them on Kubernetes and own them in production."
        ),
        responsibilities=_BE_DO,
        requirements=_BE_NEED,
        nice_to_have=_BE_NICE,
        description_en=_description(_BE_ABOUT, _BE_DO, _BE_NEED, _BE_NICE),
        description_ar=_description_ar(
            "ستبني وتشغل الخدمات التي تخطط عمليات التوصيل لعملائنا في الإمارات. ستنضم إلى "
            "فريق من ستة مهندسين يتولون خدماتهم من التصميم إلى التشغيل.",
            [
                "تصميم خدمات وواجهات برمجية بلغة Python وبناؤها واختبارها.",
                "تشغيل الخدمات على Kubernetes والحفاظ على سلامتها.",
                "مراجعة الشيفرة ومساعدة الزملاء على التطور.",
            ],
            [
                "خبرة 5 سنوات في بناء خدمات خلفية بلغة Python.",
                "خبرة في تشغيل خدمات على Kubernetes في بيئة الإنتاج.",
                "معرفة جيدة بقواعد بيانات PostgreSQL ولغة SQL.",
            ],
            ["خبرة في بث الأحداث مثل Kafka.", "إجادة اللغة العربية."],
        ),
    ),
    rubric=Rubric(
        job_title="Senior Backend Engineer",
        must_haves=[
            Criterion(
                id="python_backend",
                name="Python backend services",
                description="Has built and maintained backend services in Python for several years.",
                evidence="Names Python services or APIs they built, with their role and how long.",
                weight=9,
            ),
            Criterion(
                id="kubernetes_production",
                name="Kubernetes in production",
                description="Has run services on Kubernetes in production.",
                evidence="Describes deploying or operating a production workload on Kubernetes.",
                weight=7,
            ),
            Criterion(
                id="postgresql",
                name="PostgreSQL and SQL",
                description="Designs schemas and writes efficient SQL for PostgreSQL.",
                evidence="Mentions PostgreSQL work such as schema design, queries or tuning.",
                weight=6,
            ),
            Criterion(
                id="written_english",
                name="Clear written English",
                description="Writes clear design documents and code reviews in English.",
                evidence="The CV and answers are clear; mentions design docs or reviews.",
                weight=4,
            ),
        ],
        nice_to_haves=[
            Criterion(
                id="event_streaming",
                name="Event streaming",
                description="Has used event streaming such as Kafka.",
                evidence="Mentions Kafka or a similar system in a project.",
                weight=3,
            ),
            Criterion(
                id="arabic",
                name="Arabic",
                description="Can work in Arabic.",
                evidence="Lists Arabic as a language they speak or write.",
                weight=2,
            ),
        ],
        knockout_questions=[
            KnockoutQuestion(
                id="right_to_work_uae",
                question="Do you have the right to work in the UAE, or will you need a visa from us?",
                question_ar="هل لديك حق العمل في الإمارات، أم ستحتاج إلى تأشيرة منا؟",
                expected_answer="yes",
                reason="We can sponsor visas for this role, but a person needs to plan it.",
            )
        ],
    ),
)

# ------------------------------------------------------------------ AP specialist

_AP_ABOUT = (
    "You will process supplier invoices for our offices in Abu Dhabi and make sure every "
    "payment is correct, approved and on time."
)
_AP_DO = [
    "Check and code supplier invoices.",
    "Match invoices to purchase orders and receipts.",
    "Prepare payment runs for approval.",
    "Answer supplier questions by email and phone.",
]
_AP_NEED = [
    "3 years of experience in accounts payable.",
    "Good knowledge of UAE VAT invoices.",
    "Experience with an ERP system such as SAP or Oracle.",
]
_AP_NICE = ["A recognised accounting qualification.", "Spoken Arabic."]

AP_SPECIALIST: Final = DemoJob(
    slug="accounts-payable-specialist",
    brief=(
        "Accounts payable specialist in Abu Dhabi, on site.\n"
        "Needs 3 years of AP, UAE VAT invoices and an ERP like SAP or Oracle.\n"
        "Accounting qualification and Arabic are a plus. Salary hidden."
    ),
    team="Finance",
    location="Abu Dhabi",
    country="AE",
    work_model="onsite",
    employment_type="full_time",
    salary_min=Decimal("12000"),
    salary_max=Decimal("16000"),
    salary_currency="AED",
    salary_period="month",
    show_salary=False,
    draft=JobDescriptionDraft(
        title="Accounts Payable Specialist",
        title_ar="أخصائي/ة حسابات دائنة",
        summary=(
            "You will process supplier invoices and prepare payments. You will make sure "
            "every payment is correct and approved."
        ),
        responsibilities=_AP_DO,
        requirements=_AP_NEED,
        nice_to_have=_AP_NICE,
        description_en=_description(_AP_ABOUT, _AP_DO, _AP_NEED, _AP_NICE),
        description_ar=_description_ar(
            "ستعالج فواتير الموردين لمكاتبنا في أبوظبي وتتأكد من أن كل دفعة صحيحة ومعتمدة وفي "
            "موعدها.",
            [
                "تدقيق فواتير الموردين وترميزها.",
                "مطابقة الفواتير مع أوامر الشراء والإيصالات.",
                "تجهيز دفعات المدفوعات للاعتماد.",
            ],
            [
                "خبرة 3 سنوات في الحسابات الدائنة.",
                "معرفة جيدة بفواتير ضريبة القيمة المضافة في الإمارات.",
                "خبرة في نظام تخطيط موارد مثل SAP أو Oracle.",
            ],
            ["مؤهل محاسبي معترف به.", "التحدث بالعربية."],
        ),
    ),
    rubric=Rubric(
        job_title="Accounts Payable Specialist",
        must_haves=[
            Criterion(
                id="ap_experience",
                name="Accounts payable experience",
                description="Has worked in accounts payable for about three years or more.",
                evidence="Names AP roles with dates and the invoice work they did.",
                weight=8,
            ),
            Criterion(
                id="uae_vat",
                name="UAE VAT invoices",
                description="Knows what a valid UAE VAT invoice needs.",
                evidence="Mentions checking tax invoices, TRNs or VAT returns in the UAE.",
                weight=7,
            ),
            Criterion(
                id="erp",
                name="ERP system",
                description="Has used an ERP system for payables.",
                evidence="Names SAP, Oracle or a similar ERP and what they did in it.",
                weight=5,
            ),
        ],
        nice_to_haves=[
            Criterion(
                id="accounting_qualification",
                name="Accounting qualification",
                description="Holds or studies for a recognised accounting qualification.",
                evidence="Lists a qualification such as ACCA, CMA or CPA.",
                weight=3,
            ),
        ],
        knockout_questions=[],
    ),
)

# ------------------------------------------------------------------ support lead

_SL_ABOUT = (
    "You will lead a team of eight support agents who help drivers and customers by chat "
    "and phone. You can work from anywhere in the UAE."
)
_SL_DO = [
    "Coach the team and plan shifts.",
    "Handle complex cases and complaints.",
    "Track response times and customer satisfaction.",
    "Improve our help articles with the product team.",
]
_SL_NEED = [
    "Experience leading a customer support team.",
    "Fluent English and Arabic, written and spoken.",
    "Experience with a help desk tool such as Zendesk or Freshdesk.",
]
_SL_NICE = ["Experience in logistics or delivery."]

SUPPORT_LEAD: Final = DemoJob(
    slug="customer-support-lead",
    brief=(
        "Customer support lead, remote within the UAE, leading 8 agents.\n"
        "Must have team lead experience, fluent English and Arabic, and a help desk tool.\n"
        "Logistics experience is a plus. Salary 18000 to 22000 AED a month, shown."
    ),
    team="Customer support",
    location="United Arab Emirates",
    country="AE",
    work_model="remote",
    employment_type="full_time",
    salary_min=Decimal("18000"),
    salary_max=Decimal("22000"),
    salary_currency="AED",
    salary_period="month",
    show_salary=True,
    draft=JobDescriptionDraft(
        title="Customer Support Lead",
        title_ar="قائد/ة فريق دعم العملاء",
        summary=(
            "You will lead our support team and help drivers and customers. You will "
            "improve how fast and how well we answer."
        ),
        responsibilities=_SL_DO,
        requirements=_SL_NEED,
        nice_to_have=_SL_NICE,
        description_en=_description(_SL_ABOUT, _SL_DO, _SL_NEED, _SL_NICE),
        description_ar=_description_ar(
            "ستقود فريقا من ثمانية موظفي دعم يساعدون السائقين والعملاء عبر المحادثة والهاتف. "
            "يمكنك العمل من أي مكان في الإمارات.",
            [
                "تدريب الفريق وتخطيط المناوبات.",
                "التعامل مع الحالات المعقدة والشكاوى.",
                "متابعة أوقات الرد ورضا العملاء.",
            ],
            [
                "خبرة في قيادة فريق دعم عملاء.",
                "إجادة العربية والإنجليزية كتابة وتحدثا.",
                "خبرة في أداة مكتب مساعدة مثل Zendesk أو Freshdesk.",
            ],
            ["خبرة في الخدمات اللوجستية أو التوصيل."],
        ),
    ),
    rubric=Rubric(
        job_title="Customer Support Lead",
        must_haves=[
            Criterion(
                id="team_leadership",
                name="Support team leadership",
                description="Has led a customer support team.",
                evidence="Names a team they led, its size and what they improved.",
                weight=9,
            ),
            Criterion(
                id="english_arabic",
                name="English and Arabic",
                description="Works fluently in English and Arabic.",
                evidence="Lists both languages and support work in both.",
                weight=7,
            ),
            Criterion(
                id="help_desk",
                name="Help desk tools",
                description="Has used a help desk tool every day.",
                evidence="Names Zendesk, Freshdesk or a similar tool.",
                weight=4,
            ),
        ],
        nice_to_haves=[
            Criterion(
                id="logistics",
                name="Logistics",
                description="Knows logistics or delivery operations.",
                evidence="Mentions work in logistics, delivery or transport.",
                weight=3,
            ),
        ],
        knockout_questions=[
            KnockoutQuestion(
                id="based_in_uae",
                question="Do you live in the UAE?",
                question_ar="هل تقيم في الإمارات؟",
                expected_answer="yes",
                reason="The role is remote but must be done from within the UAE.",
            )
        ],
    ),
)

DEMO_JOBS: Final[tuple[DemoJob, ...]] = (BACKEND, AP_SPECIALIST, SUPPORT_LEAD)
