# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Vocabulary for synthetic vendors and invoices: spend categories with line items in
English and Arabic, name syllables, places and people.

Company names are built from invented syllables (``Zorvel``, ``Kantiro``), never taken
from a list of real companies. Street and building names are made up; city and area
names are real places so addresses look plausible.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from synth.pay.models import VatCategory


@dataclass(frozen=True)
class Item:
    en: str
    ar: str
    unit: str
    low: float
    high: float
    vat: VatCategory = "S"
    qty: str = "int"  # int | dec | one | bulk


@dataclass(frozen=True)
class Category:
    key: str
    suffix_en: str
    suffix_ar: str
    goods: bool
    items: tuple[Item, ...]


CATEGORIES: Final[tuple[Category, ...]] = (
    Category(
        "it_hardware",
        "IT Solutions",
        "لحلول تقنية المعلومات",
        True,
        (
            Item("Business laptop 14 inch", "حاسوب محمول للأعمال 14 بوصة", "EA", 2900, 5200),
            Item("27 inch monitor", "شاشة 27 بوصة", "EA", 650, 1400),
            Item("Docking station", "محطة توصيل", "EA", 380, 720),
            Item("Network switch 24 port", "محول شبكة 24 منفذ", "EA", 1100, 2600),
            Item("Wireless keyboard and mouse set", "طقم لوحة مفاتيح وفأرة لاسلكي", "EA", 95, 240),
            Item("Hardware installation", "تركيب الأجهزة", "HR", 150, 320, "S", "dec"),
        ),
    ),
    Category(
        "software",
        "Software",
        "للبرمجيات",
        False,
        (
            Item("Annual software subscription", "اشتراك برمجيات سنوي", "USR", 240, 1800),
            Item("Implementation services", "خدمات التنفيذ", "DAY", 1800, 4200, "S", "dec"),
            Item("Premium support plan", "خطة دعم مميزة", "EA", 3000, 12000, "S", "one"),
            Item("Data migration", "ترحيل البيانات", "EA", 2500, 9000, "S", "one"),
            Item("User training session", "جلسة تدريب المستخدمين", "SES", 900, 2400),
        ),
    ),
    Category(
        "office_supplies",
        "Office Supplies Trading",
        "لتجارة اللوازم المكتبية",
        True,
        (
            Item(
                "A4 copy paper, box of 5 reams",
                "ورق تصوير A4 علبة 5 رزم",
                "BOX",
                85,
                140,
                "S",
                "bulk",
            ),
            Item("Toner cartridge, black", "خرطوشة حبر أسود", "EA", 210, 480),
            Item("Ballpoint pens, pack of 50", "أقلام حبر جاف عبوة 50", "PK", 35, 70, "S", "bulk"),
            Item("Lever arch files", "ملفات أرشيف", "EA", 9, 18, "S", "bulk"),
            Item("Ergonomic office chair", "كرسي مكتب مريح", "EA", 650, 1600),
            Item("Whiteboard 120 x 90 cm", "سبورة بيضاء 120 x 90 سم", "EA", 220, 480),
        ),
    ),
    Category(
        "facilities",
        "Facilities Management",
        "لإدارة المرافق",
        False,
        (
            Item("Monthly office cleaning", "تنظيف المكاتب الشهري", "MON", 4500, 14000, "S", "one"),
            Item("HVAC preventive maintenance", "صيانة وقائية للتكييف", "VIS", 900, 2600),
            Item("Pest control treatment", "مكافحة الحشرات", "VIS", 350, 900),
            Item("Plumbing repair call out", "إصلاح السباكة", "HR", 120, 260, "S", "dec"),
            Item("Waste collection", "جمع النفايات", "MON", 800, 2200, "S", "one"),
        ),
    ),
    Category(
        "logistics",
        "Logistics",
        "للخدمات اللوجستية",
        False,
        (
            Item("Local delivery, Dubai", "توصيل محلي، دبي", "TRP", 120, 380),
            Item("International air freight", "شحن جوي دولي", "KG", 9, 26, "Z", "bulk"),
            Item("International sea freight", "شحن بحري دولي", "CBM", 140, 420, "Z"),
            Item("Warehouse storage", "تخزين في المستودع", "PAL", 45, 95, "S", "bulk"),
            Item("Customs clearance handling", "التخليص الجمركي", "EA", 350, 900),
        ),
    ),
    Category(
        "catering",
        "Catering Services",
        "لخدمات التموين",
        False,
        (
            Item("Staff lunch buffet", "بوفيه غداء للموظفين", "PAX", 45, 110, "S", "bulk"),
            Item("Coffee and tea service", "خدمة القهوة والشاي", "DAY", 180, 420),
            Item("Event catering package", "باقة تموين فعالية", "EA", 3500, 12000, "S", "one"),
            Item("Bottled water, case of 24", "مياه معبأة كرتونة 24", "CS", 18, 32, "S", "bulk"),
        ),
    ),
    Category(
        "consulting",
        "Management Consultancy",
        "للاستشارات الإدارية",
        False,
        (
            Item("Consulting services", "خدمات استشارية", "DAY", 3200, 7500, "S", "dec"),
            Item("Process review workshop", "ورشة مراجعة العمليات", "EA", 6000, 18000, "S", "one"),
            Item("Project management", "إدارة المشروع", "HR", 380, 750, "S", "dec"),
            Item("Report and recommendations", "تقرير وتوصيات", "EA", 5000, 15000, "S", "one"),
        ),
    ),
    Category(
        "marketing",
        "Advertising",
        "للدعاية والإعلان",
        False,
        (
            Item(
                "Social media campaign management",
                "إدارة حملة وسائل التواصل",
                "MON",
                6000,
                18000,
                "S",
                "one",
            ),
            Item("Brochure design", "تصميم كتيب", "EA", 1800, 4800, "S", "one"),
            Item("Roll up banner printing", "طباعة لافتة متحركة", "EA", 180, 420),
            Item("Photography half day", "تصوير نصف يوم", "EA", 2200, 4800),
            Item("Video production", "إنتاج فيديو", "EA", 8000, 25000, "S", "one"),
        ),
    ),
    Category(
        "construction",
        "Building Materials Trading",
        "لتجارة مواد البناء",
        True,
        (
            Item("Gypsum board 12.5 mm", "ألواح جبس 12.5 مم", "SHT", 22, 38, "S", "bulk"),
            Item("Ceramic floor tiles", "بلاط أرضيات سيراميك", "SQM", 38, 95, "S", "bulk"),
            Item("Interior emulsion paint, 18 L", "دهان داخلي 18 لتر", "DRM", 160, 320),
            Item("Steel door frame", "إطار باب فولاذي", "EA", 420, 980),
            Item("LED panel light 60 x 60", "لوحة إضاءة LED 60 x 60", "EA", 95, 210, "S", "bulk"),
        ),
    ),
    Category(
        "telecom",
        "Telecom Services",
        "لخدمات الاتصالات",
        False,
        (
            Item(
                "Business fibre line 500 Mbps",
                "خط ألياف للأعمال 500 ميغابت",
                "MON",
                1400,
                3800,
                "S",
                "one",
            ),
            Item("Mobile plan, corporate", "باقة جوال للشركات", "LN", 120, 350, "S", "bulk"),
            Item("SIP trunk channels", "قنوات اتصال SIP", "CH", 60, 140),
            Item("Router rental", "استئجار جهاز توجيه", "MON", 80, 220, "S", "one"),
        ),
    ),
    Category(
        "printing",
        "Printing Press",
        "للطباعة والنشر",
        True,
        (
            Item("Business cards, box of 500", "بطاقات أعمال علبة 500", "BOX", 90, 220),
            Item("Letterhead printing", "طباعة أوراق رسمية", "REM", 120, 260),
            Item("Annual report printing", "طباعة التقرير السنوي", "EA", 28, 75, "S", "bulk"),
            Item("Branded envelopes", "مظاريف مطبوعة", "BOX", 140, 300),
        ),
    ),
    Category(
        "security",
        "Security Services",
        "لخدمات الأمن",
        False,
        (
            Item("Security guard services", "خدمات حراسة أمنية", "HR", 28, 55, "S", "bulk"),
            Item("CCTV maintenance", "صيانة كاميرات المراقبة", "VIS", 600, 1600),
            Item("Access control cards", "بطاقات التحكم بالدخول", "EA", 12, 28, "S", "bulk"),
            Item(
                "Security system monitoring", "مراقبة النظام الأمني", "MON", 1200, 3200, "S", "one"
            ),
        ),
    ),
)
CATEGORY_BY_KEY: Final = {c.key: c for c in CATEGORIES}

UNIT_AR: Final = {
    "EA": "قطعة",
    "HR": "ساعة",
    "DAY": "يوم",
    "USR": "مستخدم",
    "SES": "جلسة",
    "BOX": "علبة",
    "PK": "عبوة",
    "MON": "شهر",
    "VIS": "زيارة",
    "TRP": "رحلة",
    "KG": "كغ",
    "CBM": "م3",
    "PAL": "منصة",
    "PAX": "شخص",
    "CS": "كرتونة",
    "SHT": "لوح",
    "SQM": "م2",
    "DRM": "برميل",
    "LN": "خط",
    "CH": "قناة",
    "REM": "رزمة",
}

# Syllables for invented company stems: combinations are checked to be pronounceable,
# not checked against real companies (none were taken from one).
ONSETS: Final = ("Z", "K", "V", "Q", "T", "M", "N", "R", "S", "D", "L", "B", "F", "H", "J")
VOWELS: Final = ("a", "o", "e", "i", "u")
CODAS: Final = ("rv", "nt", "lv", "x", "rq", "nd", "rm", "z", "lt", "sk", "v", "r", "n")
ENDINGS: Final = ("el", "iro", "ana", "ix", "ora", "en", "ova", "us", "ia", "ar", "eth", "ino")

LATIN_TO_AR: Final = {
    "a": "ا",  # noqa: RUF001
    "b": "ب",
    "d": "د",
    "e": "ي",
    "f": "ف",
    "h": "ه",  # noqa: RUF001
    "i": "ي",
    "j": "ج",
    "k": "ك",
    "l": "ل",
    "m": "م",
    "n": "ن",
    "o": "و",
    "q": "ق",
    "r": "ر",
    "s": "س",
    "t": "ت",
    "u": "و",
    "v": "ف",
    "x": "كس",
    "z": "ز",
    "g": "غ",
    "p": "ب",
    "c": "ك",
    "w": "و",
    "y": "ي",
}


def to_arabic(stem: str) -> str:
    """A rough Arabic spelling of an invented Latin stem (synthetic, not a standard)."""
    out = []
    for i, ch in enumerate(stem.lower()):
        letter = LATIN_TO_AR.get(ch, "")
        # Short vowels inside a word are usually not written.
        if ch in "aeiou" and 0 < i < len(stem) - 1 and stem[i - 1].lower() not in "aeiou":
            if ch in "ei" and i == len(stem) - 2:
                out.append(letter)
            continue
        out.append(letter)
    return "".join(out)


UAE_PLACES: Final = (
    ("Dubai", "Dubai", "Al Quoz Industrial Area 3"),
    ("Dubai", "Dubai", "Business Bay"),
    ("Dubai", "Dubai", "Deira, Port Saeed"),
    ("Dubai", "Dubai", "Jebel Ali Free Zone"),
    ("Dubai", "Dubai", "Dubai Investments Park"),
    ("Abu Dhabi", "Abu Dhabi", "Mussafah Industrial Area"),
    ("Abu Dhabi", "Abu Dhabi", "Al Reem Island"),
    ("Sharjah", "Sharjah", "Industrial Area 6"),
    ("Sharjah", "Sharjah", "Al Majaz 2"),
    ("Ajman", "Ajman", "Al Jurf Industrial 1"),
    ("Ras Al Khaimah", "Ras Al Khaimah", "Al Hamra"),
)
INTL_PLACES: Final = {
    "GB": (("Leeds", "West Yorkshire"), ("Bristol", "Bristol")),
    "DE": (("Hamburg", "Hamburg"), ("Stuttgart", "Baden-Wurttemberg")),
    "FR": (("Lyon", "Auvergne-Rhone-Alpes"),),
    "NL": (("Utrecht", "Utrecht"),),
    "SA": (("Riyadh", "Riyadh Province"),),
    "BH": (("Manama", "Capital Governorate"),),
}
STREET_WORDS: Final = (
    "Sidra",
    "Ghaf",
    "Lulu",
    "Yasmin",
    "Reem",
    "Oryx",
    "Corniche Link",
    "Palm Grove",
    "Falaj",
    "Nakheel Garden",
)
BUILDING_WORDS: Final = ("Tower", "Building", "Warehouse", "Business Centre", "Plaza", "House")

FIRST_NAMES: Final = (
    "Aisha",
    "Omar",
    "Priya",
    "Daniel",
    "Fatima",
    "Rahul",
    "Mariam",
    "Hassan",
    "Elena",
    "Joseph",
    "Noura",
    "Karim",
    "Sara",
    "Imran",
    "Layla",
    "Thomas",
    "Huda",
    "Vikram",
    "Grace",
    "Yousef",
)
LAST_NAMES: Final = (
    "Haddad",
    "Menon",
    "Fernandes",
    "Rahman",
    "Kowalski",
    "Nasser",
    "Pillai",
    "Okafor",
    "Saleh",
    "Brennan",
    "Qureshi",
    "Mansour",
    "Silva",
    "Iyer",
    "Darwish",
    "Novak",
)
ROLES: Final = ("Accounts Receivable", "Billing", "Sales", "Finance Manager", "Operations")
DEPARTMENTS: Final = ("Operations", "IT", "Facilities", "Marketing", "Finance", "Logistics", "HR")

BUYER_NAME: Final = "Pairly Demo Trading LLC"
BUYER_NAME_AR: Final = "بيرلي ديمو للتجارة ذ.م.م"
BUYER_TRN: Final = "190000000000001"
BUYER_DOMAIN: Final = "pairly-demo.example"
AP_INBOX: Final = f"ap@{BUYER_DOMAIN}"
BUYER_ADDRESS: Final = ("Office 1204, Sidra Tower", "Business Bay, Dubai, UAE")
