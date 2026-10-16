# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
# ruff: noqa: E501  (template sentences read better on one line)
"""Vocabulary for synthetic CVs: role families, titles, skills and achievement lines.

Everything here is made up for test data. Product names (SAP, PostgreSQL, ...) are
skills, not people or organisations' private data. Each achievement template carries
``tags`` (the capabilities it demonstrates) and ``skills`` (the canonical skill names
it shows in use): the rubric labeler (``synth.hire.labels``) scores criteria from these,
never from free text, so the labels are reproducible.

Templates have an English and an Arabic version with the same placeholders.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

LEVELS: Final = ("junior", "mid", "senior", "lead")


@dataclass(frozen=True)
class Template:
    en: str
    ar: str
    tags: tuple[str, ...] = ()
    skills: tuple[str, ...] = ()
    # Placeholder -> choices, picked with the CV's random generator.
    slots: tuple[tuple[str, tuple[str, ...]], ...] = ()
    # Lowest level that writes this line (lead lines are not written by juniors).
    min_level: str = "junior"


@dataclass(frozen=True)
class Family:
    key: str
    titles: dict[str, tuple[str, str]]  # level -> (en, ar)
    core_skills: tuple[str, ...]
    extra_skills: tuple[str, ...]
    templates: tuple[Template, ...]
    summary_en: str
    summary_ar: str
    degree_fields: tuple[str, ...]
    certifications: tuple[tuple[str, str], ...] = ()  # (name, issuer)


N = ("120", "250", "400", "800", "1,200", "2,500", "4,000")
PCT = ("18", "25", "30", "40", "55", "60")

BACKEND = Family(
    key="backend",
    titles={
        "junior": ("Junior Backend Developer", "مطور أنظمة خلفية مبتدئ"),
        "mid": ("Backend Engineer", "مهندس أنظمة خلفية"),
        "senior": ("Senior Backend Engineer", "مهندس أنظمة خلفية أول"),
        "lead": ("Lead Backend Engineer", "مهندس أنظمة خلفية رئيسي"),
    },
    core_skills=("Python", "Go", "PostgreSQL", "REST APIs", "Docker", "Redis"),
    extra_skills=("Kubernetes", "AWS", "Kafka", "gRPC", "CI/CD", "unit testing", "Linux", "Git"),
    templates=(
        Template(
            "Built REST APIs in {lang} and PostgreSQL serving {n} requests per minute.",
            "بنيت واجهات REST APIs بلغة {lang} وقاعدة PostgreSQL تخدم {n} طلب في الدقيقة.",
            ("api_design", "databases"),
            ("{lang}", "PostgreSQL", "REST APIs"),
            (("lang", ("Python", "Go")), ("n", N)),
        ),
        Template(
            "Wrote gRPC services in {lang} for the orders platform.",
            "كتبت خدمات gRPC بلغة {lang} لمنصة الطلبات.",
            ("api_design",),
            ("{lang}", "gRPC"),
            (("lang", ("Python", "Go", "Java")),),
        ),
        Template(
            "Designed an event pipeline on Kafka that processes {n} thousand messages a day.",
            "صممت مسار أحداث على Kafka يعالج {n} ألف رسالة يوميا.",
            ("distributed_systems",),
            ("Kafka",),
            (("n", N),),
            "mid",
        ),
        Template(
            "Moved {k} services to Docker and Kubernetes on AWS and cut deploy time by {p}%.",
            "نقلت {k} خدمات إلى Docker وKubernetes على AWS وخفضت زمن النشر بنسبة {p}%.",
            ("cloud_ops",),
            ("Docker", "Kubernetes", "AWS"),
            (("k", ("4", "6", "9", "12")), ("p", PCT)),
        ),
        Template(
            "Raised unit test coverage from {a}% to {b}% and added CI/CD checks to every merge.",
            "رفعت تغطية اختبارات الوحدات من {a}% إلى {b}% وأضفت فحوص CI/CD لكل دمج.",
            ("testing",),
            ("unit testing", "CI/CD"),
            (("a", ("35", "42", "50")), ("b", ("78", "85", "90"))),
        ),
        Template(
            "Cut p95 latency of the search API from {a} ms to {b} ms with Redis caching.",
            "خفضت زمن الاستجابة لواجهة البحث من {a} إلى {b} ملي ثانية باستخدام Redis.",
            ("performance",),
            ("Redis",),
            (("a", ("480", "650", "900")), ("b", ("90", "120", "160"))),
        ),
        Template(
            "Led the design review of a payments ledger that records {n} transactions a day.",
            "قدت مراجعة تصميم سجل مدفوعات يسجل {n} معاملة يوميا.",
            ("system_design", "leadership"),
            (),
            (("n", N),),
            "senior",
        ),
        Template(
            "Mentored {k} junior engineers through code reviews and pairing.",
            "وجهت {k} مهندسين مبتدئين من خلال مراجعة الشيفرة والبرمجة الثنائية.",
            ("mentoring",),
            (),
            (("k", ("2", "3", "4", "5")),),
            "senior",
        ),
        Template(
            "Fixed production incidents on call and wrote the postmortems.",
            "عالجت أعطال بيئة الإنتاج ضمن المناوبة وكتبت تقارير ما بعد الحادثة.",
            ("operations",),
            ("Linux",),
        ),
    ),
    summary_en="Backend engineer with {years_en} of experience building APIs and services.",
    summary_ar="مهندس أنظمة خلفية بخبرة {years} سنوات في بناء الواجهات والخدمات.",
    degree_fields=("Computer Science", "Software Engineering", "Computer Engineering"),
    certifications=(("AWS Certified Developer", "Amazon Web Services"),),
)

FRONTEND = Family(
    key="frontend",
    titles={
        "junior": ("Junior Frontend Developer", "مطور واجهات أمامية مبتدئ"),
        "mid": ("Frontend Engineer", "مهندس واجهات أمامية"),
        "senior": ("Senior Frontend Engineer", "مهندس واجهات أمامية أول"),
        "lead": ("Lead Frontend Engineer", "مهندس واجهات أمامية رئيسي"),
    },
    core_skills=("TypeScript", "React", "CSS", "REST APIs"),
    extra_skills=("Next.js", "Jest", "accessibility", "Figma", "Node.js", "Git"),
    templates=(
        Template(
            "Built the checkout screens in React and TypeScript for {n} daily users.",
            "بنيت شاشات الدفع باستخدام React وTypeScript لعدد {n} مستخدم يوميا.",
            ("ui",),
            ("React", "TypeScript"),
            (("n", N),),
        ),
        Template(
            "Connected the web app to REST APIs and cut page load time by {p}%.",
            "ربطت تطبيق الويب بواجهات REST APIs وخفضت زمن تحميل الصفحات بنسبة {p}%.",
            ("api_integration",),
            ("REST APIs",),
            (("p", PCT),),
        ),
        Template(
            "Wrote a Node.js backend for form submissions with PostgreSQL storage.",
            "كتبت خدمة Node.js لاستقبال النماذج مع تخزين في PostgreSQL.",
            ("api_design", "databases"),
            ("Node.js", "PostgreSQL"),
            (),
            "mid",
        ),
        Template(
            "Fixed accessibility issues across {k} pages to meet WCAG AA.",
            "أصلحت مشكلات إمكانية الوصول في {k} صفحة لتوافق معيار WCAG AA.",
            ("accessibility",),
            ("accessibility",),
            (("k", ("12", "30", "45")),),
        ),
        Template(
            "Added Jest tests and CI/CD checks to the design system.",
            "أضفت اختبارات Jest وفحوص CI/CD إلى نظام التصميم.",
            ("testing",),
            ("Jest", "CI/CD"),
        ),
    ),
    summary_en="Frontend engineer with {years_en} of experience building web interfaces.",
    summary_ar="مهندس واجهات أمامية بخبرة {years} سنوات في بناء واجهات الويب.",
    degree_fields=("Computer Science", "Information Technology", "Graphic Design"),
)

ACCOUNTS_PAYABLE = Family(
    key="accounts_payable",
    titles={
        "junior": ("Accounts Payable Assistant", "مساعد حسابات دائنة"),
        "mid": ("Accounts Payable Accountant", "محاسب حسابات دائنة"),
        "senior": ("Senior Accounts Payable Accountant", "محاسب حسابات دائنة أول"),
        "lead": ("Accounts Payable Supervisor", "مشرف حسابات دائنة"),
    },
    core_skills=(
        "invoice processing",
        "three-way matching",
        "VAT",
        "Excel",
        "vendor reconciliation",
    ),
    extra_skills=("SAP", "Oracle Fusion", "Microsoft Dynamics", "payment runs", "accruals", "IFRS"),
    templates=(
        Template(
            "Processed about {n} supplier invoices a month in {erp} with a {p}% first-pass match rate.",
            "عالجت نحو {n} فاتورة موردين شهريا في نظام {erp} بنسبة مطابقة أولى {p}%.",
            ("invoice_processing",),
            ("invoice processing", "{erp}"),
            (("n", N), ("erp", ("SAP", "Oracle Fusion", "Microsoft Dynamics")), ("p", PCT)),
        ),
        Template(
            "Ran three-way matching of purchase orders, goods receipts and invoices for {k} vendors.",
            "طبقت المطابقة الثلاثية بين أوامر الشراء وإيصالات الاستلام والفواتير لعدد {k} مورد.",
            ("three_way_match", "invoice_processing"),
            ("three-way matching",),
            (("k", ("60", "150", "300")),),
        ),
        Template(
            "Prepared VAT workings and checked tax invoices against UAE VAT rules.",
            "أعددت حسابات ضريبة القيمة المضافة وراجعت الفواتير الضريبية وفق قواعد الضريبة في الإمارات.",
            ("vat",),
            ("VAT",),
        ),
        Template(
            "Reconciled vendor statements every month and cleared {k} aged items.",
            "طابقت كشوف حسابات الموردين شهريا وسويت {k} بندا متأخرا.",
            ("reconciliation",),
            ("vendor reconciliation",),
            (("k", ("40", "85", "130")),),
        ),
        Template(
            "Owned the weekly payment runs of AED {m} million with dual approval.",
            "توليت دفعات السداد الأسبوعية بقيمة {m} مليون درهم مع اعتماد مزدوج.",
            ("payment_runs",),
            ("payment runs",),
            (("m", ("2", "5", "12")),),
            "mid",
        ),
        Template(
            "Cut the month-end payables close from {a} to {b} working days.",
            "خفضت مدة إقفال الحسابات الدائنة في نهاية الشهر من {a} إلى {b} أيام عمل.",
            ("close",),
            ("accruals",),
            (("a", ("8", "7", "6")), ("b", ("4", "3"))),
            "senior",
        ),
        Template(
            "Checked every vendor bank detail change with a call-back before payment.",
            "تحققت من كل تغيير في البيانات البنكية للموردين باتصال هاتفي قبل الدفع.",
            ("fraud_controls",),
            (),
        ),
        Template(
            "Built Excel models for accruals and aged payables reports.",
            "بنيت نماذج Excel للمستحقات وتقارير أعمار الذمم الدائنة.",
            ("reporting",),
            ("Excel", "accruals"),
        ),
        Template(
            "Trained and supervised a team of {k} AP clerks.",
            "دربت فريقا من {k} موظفي حسابات دائنة وأشرفت عليه.",
            ("leadership",),
            (),
            (("k", ("3", "4", "6")),),
            "lead",
        ),
    ),
    summary_en="Accounts payable professional with {years_en} of experience in invoice processing.",
    summary_ar="محاسب حسابات دائنة بخبرة {years} سنوات في معالجة الفواتير.",
    degree_fields=("Accounting", "Finance", "Commerce"),
    certifications=(
        ("ACCA", "Association of Chartered Certified Accountants"),
        ("CMA", "Institute of Management Accountants"),
    ),
)

ACCOUNTANT = Family(
    key="accountant",
    titles={
        "junior": ("Junior Accountant", "محاسب مبتدئ"),
        "mid": ("General Accountant", "محاسب عام"),
        "senior": ("Senior Accountant", "محاسب أول"),
        "lead": ("Finance Manager", "مدير مالي"),
    },
    core_skills=("general ledger", "Excel", "IFRS", "bank reconciliation"),
    extra_skills=("VAT", "SAP", "budgeting", "audit support", "accruals"),
    templates=(
        Template(
            "Closed the general ledger every month for {k} entities under IFRS.",
            "أقفلت دفتر الأستاذ العام شهريا لعدد {k} منشآت وفق المعايير الدولية IFRS.",
            ("close",),
            ("general ledger", "IFRS"),
            (("k", ("2", "3", "5")),),
        ),
        Template(
            "Prepared bank reconciliations and fixed {k} long-standing differences.",
            "أعددت التسويات البنكية وعالجت {k} فروقات قديمة.",
            ("reconciliation",),
            ("bank reconciliation",),
            (("k", ("15", "30", "60")),),
        ),
        Template(
            "Filed quarterly VAT returns with the Federal Tax Authority.",
            "قدمت إقرارات ضريبة القيمة المضافة الفصلية إلى الهيئة الاتحادية للضرائب.",
            ("vat",),
            ("VAT",),
        ),
        Template(
            "Supported the external audit and prepared {k} lead schedules.",
            "دعمت التدقيق الخارجي وأعددت {k} جدولا رئيسيا.",
            ("audit",),
            ("audit support",),
            (("k", ("12", "20", "35")),),
        ),
        Template(
            "Posted supplier invoices in SAP when the AP team was short staffed.",
            "سجلت فواتير الموردين في نظام SAP عند نقص موظفي الحسابات الدائنة.",
            ("invoice_processing",),
            ("SAP", "invoice processing"),
        ),
    ),
    summary_en="Accountant with {years_en} of experience in month-end close and reporting.",
    summary_ar="محاسب بخبرة {years} سنوات في الإقفال الشهري وإعداد التقارير.",
    degree_fields=("Accounting", "Finance", "Business Administration"),
    certifications=(("CPA", "American Institute of CPAs"),),
)

RECRUITING = Family(
    key="recruiting",
    titles={
        "junior": ("Recruitment Coordinator", "منسق توظيف"),
        "mid": ("Recruiter", "أخصائي توظيف"),
        "senior": ("Senior Recruiter", "أخصائي توظيف أول"),
        "lead": ("Talent Acquisition Lead", "قائد فريق استقطاب المواهب"),
    },
    core_skills=("full-cycle recruiting", "sourcing", "LinkedIn Recruiter", "interviewing"),
    extra_skills=("Greenhouse", "Workday", "offer negotiation", "employer branding", "Excel"),
    templates=(
        Template(
            "Hired {k} engineers and data analysts in {y} across the UAE and Saudi Arabia.",
            "وظفت {k} مهندسا ومحلل بيانات خلال {y} في الإمارات والسعودية.",
            ("tech_hiring", "hires_volume"),
            (),
            (("k", ("18", "35", "60")), ("y", ("2023", "2024", "2025"))),
        ),
        Template(
            "Ran full-cycle recruiting for up to {k} open roles at a time, from intake to offer.",
            "أدرت عملية التوظيف كاملة لما يصل إلى {k} وظيفة شاغرة في الوقت نفسه من الطلب حتى العرض.",
            ("full_cycle",),
            ("full-cycle recruiting",),
            (("k", ("12", "20", "30")),),
        ),
        Template(
            "Sourced passive candidates on LinkedIn Recruiter with a {p}% reply rate.",
            "استقطبت مرشحين غير باحثين عن عمل عبر LinkedIn Recruiter بنسبة رد {p}%.",
            ("sourcing",),
            ("sourcing", "LinkedIn Recruiter"),
            (("p", PCT),),
        ),
        Template(
            "Cut time to hire from {a} to {b} days by restructuring interview loops.",
            "خفضت مدة التوظيف من {a} إلى {b} يوما بإعادة تنظيم مراحل المقابلات.",
            ("time_to_hire",),
            ("interviewing",),
            (("a", ("52", "45", "60")), ("b", ("31", "28", "35"))),
            "mid",
        ),
        Template(
            "Managed the {ats} ATS and trained {k} hiring managers on structured interviews.",
            "أدرت نظام تتبع المتقدمين {ats} ودربت {k} من مديري التوظيف على المقابلات المنظمة.",
            ("ats", "structured_interviews"),
            ("{ats}", "interviewing"),
            (("ats", ("Greenhouse", "Workday")), ("k", ("10", "25", "40"))),
        ),
        Template(
            "Led high-volume hiring of {k} customer service staff for a new site.",
            "قدت توظيفا بأعداد كبيرة شمل {k} موظف خدمة عملاء لموقع جديد.",
            ("high_volume", "hires_volume"),
            (),
            (("k", ("80", "150", "220")),),
            "mid",
        ),
        Template(
            "Negotiated offers and kept the offer acceptance rate at {p}%.",
            "تفاوضت على عروض العمل وحافظت على نسبة قبول العروض عند {p}%.",
            ("offers",),
            ("offer negotiation",),
            (("p", ("82", "88", "91")),),
        ),
        Template(
            "Built the Emiratisation hiring plan and met the yearly target.",
            "وضعت خطة التوطين في التوظيف وحققت المستهدف السنوي.",
            ("emiratisation",),
            (),
            (),
            "senior",
        ),
    ),
    summary_en="Recruiter with {years_en} of experience hiring across the Gulf.",
    summary_ar="أخصائي توظيف بخبرة {years} سنوات في التوظيف في دول الخليج.",
    degree_fields=("Human Resources", "Psychology", "Business Administration"),
    certifications=(("SHRM-CP", "Society for Human Resource Management"), ("CIPD Level 5", "CIPD")),
)

HR = Family(
    key="hr_generalist",
    titles={
        "junior": ("HR Assistant", "مساعد موارد بشرية"),
        "mid": ("HR Generalist", "أخصائي موارد بشرية"),
        "senior": ("Senior HR Business Partner", "شريك أعمال أول للموارد البشرية"),
        "lead": ("HR Manager", "مدير موارد بشرية"),
    },
    core_skills=("employee relations", "onboarding", "UAE labour law", "Excel"),
    extra_skills=("payroll", "Workday", "performance management", "interviewing"),
    templates=(
        Template(
            "Onboarded {k} new joiners a year and ran their visa paperwork.",
            "أتممت تعيين {k} موظفا جديدا سنويا وتابعت إجراءات تأشيراتهم.",
            ("onboarding",),
            ("onboarding",),
            (("k", ("60", "120", "200")),),
        ),
        Template(
            "Screened applicants and scheduled interviews for {k} roles.",
            "فرزت طلبات المتقدمين ونظمت المقابلات لعدد {k} وظيفة.",
            ("screening",),
            ("interviewing",),
            (("k", ("15", "25", "40")),),
        ),
        Template(
            "Handled employee relations cases under UAE labour law.",
            "تعاملت مع قضايا علاقات الموظفين وفق قانون العمل الإماراتي.",
            ("employee_relations",),
            ("employee relations", "UAE labour law"),
        ),
        Template(
            "Moved performance reviews into Workday for {k} staff.",
            "نقلت تقييمات الأداء إلى نظام Workday لعدد {k} موظف.",
            ("hr_systems",),
            ("Workday", "performance management"),
            (("k", ("300", "800", "1,500")),),
        ),
    ),
    summary_en="HR professional with {years_en} of experience in employee relations and onboarding.",
    summary_ar="أخصائي موارد بشرية بخبرة {years} سنوات في علاقات الموظفين والتعيين.",
    degree_fields=("Human Resources", "Business Administration", "Law"),
    certifications=(("CIPD Level 5", "CIPD"),),
)

DATA_ANALYST = Family(
    key="data_analyst",
    titles={
        "junior": ("Junior Data Analyst", "محلل بيانات مبتدئ"),
        "mid": ("Data Analyst", "محلل بيانات"),
        "senior": ("Senior Data Analyst", "محلل بيانات أول"),
        "lead": ("Analytics Lead", "قائد فريق التحليلات"),
    },
    core_skills=("SQL", "Python", "Power BI", "Excel"),
    extra_skills=("Tableau", "dbt", "statistics", "forecasting", "PostgreSQL"),
    templates=(
        Template(
            "Built Power BI dashboards on SQL models for {k} business teams.",
            "بنيت لوحات Power BI على نماذج SQL لعدد {k} فرق عمل.",
            ("reporting",),
            ("Power BI", "SQL"),
            (("k", ("3", "5", "8")),),
        ),
        Template(
            "Automated weekly reports in Python and saved {k} hours a month.",
            "أتممت التقارير الأسبوعية آليا بلغة Python ووفرت {k} ساعة شهريا.",
            ("automation",),
            ("Python",),
            (("k", ("20", "40", "60")),),
        ),
        Template(
            "Wrote dbt models on PostgreSQL for the finance data mart.",
            "كتبت نماذج dbt على PostgreSQL لمستودع بيانات المالية.",
            ("databases",),
            ("dbt", "PostgreSQL", "SQL"),
            (),
            "mid",
        ),
        Template(
            "Forecast monthly demand with an error under {p}%.",
            "توقعت الطلب الشهري بنسبة خطأ أقل من {p}%.",
            ("forecasting",),
            ("forecasting", "statistics"),
            (("p", ("5", "8", "10")),),
        ),
    ),
    summary_en="Data analyst with {years_en} of experience in reporting and forecasting.",
    summary_ar="محلل بيانات بخبرة {years} سنوات في التقارير والتنبؤ.",
    degree_fields=("Statistics", "Mathematics", "Economics", "Computer Science"),
)

SUPPORT = Family(
    key="customer_support",
    titles={
        "junior": ("Customer Support Agent", "موظف دعم العملاء"),
        "mid": ("Customer Support Specialist", "أخصائي دعم العملاء"),
        "senior": ("Senior Support Specialist", "أخصائي دعم أول"),
        "lead": ("Support Team Lead", "قائد فريق الدعم"),
    },
    core_skills=("customer service", "Zendesk", "complaint handling"),
    extra_skills=("Excel", "CRM", "quality assurance", "training"),
    templates=(
        Template(
            "Answered about {k} customer tickets a day in Zendesk with a {p}% satisfaction score.",
            "رددت على نحو {k} تذكرة عملاء يوميا في Zendesk بنسبة رضا {p}%.",
            ("ticket_volume",),
            ("Zendesk", "customer service"),
            (("k", ("40", "60", "80")), ("p", ("88", "92", "95"))),
        ),
        Template(
            "Resolved escalated complaints and wrote the reply templates.",
            "عالجت الشكاوى المصعدة وكتبت نماذج الردود.",
            ("escalations",),
            ("complaint handling",),
        ),
        Template(
            "Trained {k} new agents on the CRM and the refund policy.",
            "دربت {k} موظفين جددا على نظام CRM وسياسة الاسترداد.",
            ("training", "leadership"),
            ("CRM", "training"),
            (("k", ("6", "10", "15")),),
            "mid",
        ),
    ),
    summary_en="Customer support specialist with {years_en} of experience in service teams.",
    summary_ar="أخصائي دعم عملاء بخبرة {years} سنوات في فرق الخدمة.",
    degree_fields=("Business Administration", "Marketing", "English Literature"),
)

FAMILIES: Final[dict[str, Family]] = {
    f.key: f
    for f in (
        BACKEND,
        FRONTEND,
        ACCOUNTS_PAYABLE,
        ACCOUNTANT,
        RECRUITING,
        HR,
        DATA_ANALYST,
        SUPPORT,
    )
}

# Families a career may move between (a backend engineer may have started in frontend).
ADJACENT: Final[dict[str, tuple[str, ...]]] = {
    "backend": ("frontend", "data_analyst"),
    "frontend": ("backend",),
    "accounts_payable": ("accountant",),
    "accountant": ("accounts_payable",),
    "recruiting": ("hr_generalist",),
    "hr_generalist": ("recruiting",),
    "data_analyst": ("backend", "accountant"),
    "customer_support": ("recruiting", "hr_generalist"),
}

# Share of CVs per family (the three sample jobs' families dominate).
FAMILY_WEIGHTS: Final[dict[str, int]] = {
    "backend": 20,
    "accounts_payable": 20,
    "recruiting": 20,
    "frontend": 8,
    "accountant": 9,
    "hr_generalist": 8,
    "data_analyst": 8,
    "customer_support": 7,
}

DEGREES: Final = {
    "bachelor": ("BSc", "بكالوريوس"),
    "master": ("MSc", "ماجستير"),
    "mba": ("MBA", "ماجستير إدارة الأعمال"),
    "diploma": ("Diploma", "دبلوم"),
}

FIELDS_AR: Final[dict[str, str]] = {
    "Computer Science": "علوم الحاسوب",
    "Software Engineering": "هندسة البرمجيات",
    "Computer Engineering": "هندسة الحاسوب",
    "Information Technology": "تقنية المعلومات",
    "Graphic Design": "التصميم الجرافيكي",
    "Accounting": "المحاسبة",
    "Finance": "التمويل",
    "Commerce": "التجارة",
    "Business Administration": "إدارة الأعمال",
    "Human Resources": "الموارد البشرية",
    "Psychology": "علم النفس",
    "Law": "القانون",
    "Statistics": "الإحصاء",
    "Mathematics": "الرياضيات",
    "Economics": "الاقتصاد",
    "Marketing": "التسويق",
    "English Literature": "الأدب الإنجليزي",
}

LANGUAGES: Final[dict[str, str]] = {
    "English": "الإنجليزية",
    "Arabic": "العربية",
    "Hindi": "الهندية",
    "Urdu": "الأردية",
    "French": "الفرنسية",
    "Tagalog": "التاغالوغية",
    "Malayalam": "المالايالامية",
    "Turkish": "التركية",
    "Spanish": "الإسبانية",
    "Yoruba": "اليوروبا",
}

LEVELS_LANG: Final[dict[str, str]] = {
    "Native": "اللغة الأم",
    "Fluent": "طلاقة",
    "Professional": "مستوى مهني",
    "Conversational": "مستوى محادثة",
    "Basic": "مستوى مبتدئ",
}

HEADINGS: Final[dict[str, tuple[str, str]]] = {
    "summary": ("Summary", "الملخص"),
    "experience": ("Experience", "الخبرة العملية"),
    "education": ("Education", "التعليم"),
    "certifications": ("Certifications", "الشهادات"),
    "skills": ("Skills", "المهارات"),
    "languages": ("Languages", "اللغات"),
    "links": ("Links", "روابط"),
    "personal": ("Personal details", "معلومات شخصية"),
}

MONTHS_EN: Final = (
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)
MONTHS_AR: Final = (
    "يناير",
    "فبراير",
    "مارس",
    "أبريل",
    "مايو",
    "يونيو",
    "يوليو",
    "أغسطس",
    "سبتمبر",
    "أكتوبر",
    "نوفمبر",
    "ديسمبر",
)

CITIES: Final = (
    ("Dubai", "دبي", "United Arab Emirates", "الإمارات العربية المتحدة"),
    ("Abu Dhabi", "أبوظبي", "United Arab Emirates", "الإمارات العربية المتحدة"),
    ("Sharjah", "الشارقة", "United Arab Emirates", "الإمارات العربية المتحدة"),
    ("Riyadh", "الرياض", "Saudi Arabia", "المملكة العربية السعودية"),
    ("Doha", "الدوحة", "Qatar", "قطر"),
    ("Muscat", "مسقط", "Oman", "عمان"),
)

COMPANY_SUFFIXES: Final = ("LLC", "FZE", "FZ-LLC", "Trading LLC", "Group", "Holdings")
