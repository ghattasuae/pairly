# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""The synthetic vendor master: 26 UAE vendors and 8 international ones.

Every value is generated from the seed: invented company names, random trade licence
numbers and TRNs (``synth.pay.banking``), IBANs at synthetic banks with valid check
digits, contacts from small name lists, and email domains under ``.example`` (RFC 2606,
reserved: no domain can belong to anyone). Each vendor also has an invoice template
(layout, font, numbering pattern, date format, terms, currency, language, usual file
formats), which the fake-invoice attacks deliberately break.
"""

from __future__ import annotations

import datetime as dt
import random
from typing import Final

from synth.hire.facts import stable_seed
from synth.pay.banking import BANKS, Bank, make_iban, make_trn
from synth.pay.catalog import (
    BUILDING_WORDS,
    CATEGORIES,
    CATEGORY_BY_KEY,
    CODAS,
    ENDINGS,
    FIRST_NAMES,
    INTL_PLACES,
    LAST_NAMES,
    ONSETS,
    ROLES,
    STREET_WORDS,
    UAE_PLACES,
    VOWELS,
    to_arabic,
)
from synth.pay.models import Address, BankDetails, Contact, Template, Vendor

UAE_COUNT: Final = 26
INTL_COUNTRIES: Final = ("GB", "GB", "DE", "DE", "FR", "NL", "SA", "BH")
INTL_SUFFIX: Final = {
    "GB": "Ltd",
    "DE": "GmbH",
    "FR": "SARL",
    "NL": "B.V.",
    "SA": "Co. Ltd",
    "BH": "W.L.L.",
}
INTL_CURRENCY: Final = {
    "GB": "GBP",
    "DE": "EUR",
    "FR": "EUR",
    "NL": "EUR",
    "SA": "SAR",
    "BH": "USD",
}
NUMBERING: Final = (
    "INV-{YYYY}-{N5}",
    "{P}{N6}",
    "{N4}/{YY}",
    "TI-{YY}{MM}-{N4}",
    "{P}-{N5}",
    "{YYYY}{N4}",
    "{P}/{YY}/{N4}",
)
DATE_FORMATS: Final = ("%d/%m/%Y", "%Y-%m-%d", "%d %b %Y", "%d-%m-%Y")
PRODUCERS: Final = ("Ledgrova", "Accuvane", "Fatorix", "Mizanto", "Billquill", "Qaydra")
ACCENTS: Final = ("#1f3a68", "#00616a", "#7a1c2c", "#2d2d2d", "#3c5a14", "#5b2a86", "#a04a00")
SCALES: Final = {
    "small": ("800.00", "6000.00"),
    "medium": ("6000.00", "40000.00"),
    "large": ("40000.00", "180000.00"),
}
# Which UAE vendors favour structured e-invoices (early PINT AE adopters).
XML_ADOPTERS: Final = frozenset({1, 4, 7, 11, 15, 19, 23})


def _weighted(rng: random.Random, weights: dict[str, int]) -> str:
    keys = sorted(weights)
    return rng.choices(keys, weights=[weights[k] for k in keys], k=1)[0]


def make_stem(rng: random.Random, used: set[str]) -> str:
    while True:
        stem = rng.choice(ONSETS) + rng.choice(VOWELS) + rng.choice(CODAS) + rng.choice(ENDINGS)
        if stem not in used and len(stem) <= 9:
            used.add(stem)
            return stem


def person(rng: random.Random) -> str:
    return f"{rng.choice(FIRST_NAMES)} {rng.choice(LAST_NAMES)}"


def uae_phone(rng: random.Random) -> str:
    area = rng.choice(("4", "2", "6", "7"))
    return f"+971 {area} {rng.randint(200, 899)} {rng.randint(1000, 9999)}"


def intl_phone(country: str, rng: random.Random) -> str:
    code = {"GB": "44", "DE": "49", "FR": "33", "NL": "31", "SA": "966", "BH": "973"}[country]
    return f"+{code} {rng.randint(10, 99)} {rng.randint(100, 999)} {rng.randint(1000, 9999)}"


def pick_bank(rng: random.Random, country: str, *, avoid: str | None = None) -> Bank:
    options = [b for b in BANKS if b.country == country and b.key != avoid]
    if not options:  # BH and SA have one synthetic bank each: fall back to a UAE bank
        options = [b for b in BANKS if b.country == "AE" and b.key != avoid]
    return rng.choice(options)


def bank_details(account_name: str, bank: Bank, rng: random.Random) -> BankDetails:
    return BankDetails(
        account_name=account_name,
        bank_name=bank.name,
        iban=make_iban(bank, rng),
        bic=bank.bic,
        country=bank.country,
    )


def _template(i: int, country: str, category: str, rng: random.Random) -> Template:
    if country == "AE":
        language = _weighted(rng, {"en": 60, "bilingual": 25, "ar": 15})
        currency = "USD" if category in ("software", "logistics") and rng.random() < 0.4 else "AED"
        if i in XML_ADOPTERS:
            formats = {"xml": 65, "pdf": 30, "scan": 5}
        elif language == "ar":
            formats = {"pdf": 45, "scan": 40, "docx": 15}
        else:
            formats = _weighted_formats(rng)
    else:
        language = "en"
        currency = INTL_CURRENCY[country]
        formats = {"pdf": 85, "docx": 15} if rng.random() < 0.5 else {"pdf": 100}
    if language == "ar":
        layout = "rtl"
    elif language == "bilingual":
        layout = "bilingual"
    else:
        layout = rng.choice(("classic", "band", "grid", "compact"))
    date_format = "%d.%m.%Y" if country == "DE" else rng.choice(DATE_FORMATS)
    if language != "en" and date_format == "%d %b %Y":
        date_format = "%d/%m/%Y"
    return Template(
        layout=layout,  # type: ignore[arg-type]
        font=rng.choice(("Helvetica", "Times-Roman", "Courier", "Helvetica")),  # type: ignore[arg-type]
        accent=rng.choice(ACCENTS),
        numbering=rng.choice(NUMBERING),
        date_format=date_format,
        payment_terms_days=rng.choice((30, 30, 45, 60, 15, 0) if country == "AE" else (30, 45)),
        currency=currency,
        language=language,  # type: ignore[arg-type]
        formats=formats,
        prints_bank=rng.random() < 0.85,
        logo=rng.choice(("circle", "square", "bar", "none")),  # type: ignore[arg-type]
        producer=f"{rng.choice(PRODUCERS)} {rng.randint(2, 9)}.{rng.randint(0, 9)}",
    )


def _weighted_formats(rng: random.Random) -> dict[str, int]:
    return rng.choice(
        (
            {"pdf": 70, "scan": 20, "docx": 10},
            {"pdf": 55, "scan": 35, "docx": 10},
            {"pdf": 80, "scan": 15, "xml": 5},
            {"pdf": 60, "scan": 25, "docx": 15},
        )
    )


def make_vendor(i: int, seed: int, used: set[str], country: str | None = None) -> Vendor:
    rng = random.Random(stable_seed(seed, "vendor", i))
    if country is None:
        country = "AE" if i < UAE_COUNT else INTL_COUNTRIES[i - UAE_COUNT]
    category = (
        CATEGORIES[i % len(CATEGORIES)].key
        if country == "AE"
        else rng.choice(("software", "consulting", "logistics", "it_hardware", "marketing"))
    )
    cat = CATEGORY_BY_KEY[category]
    stem = make_stem(rng, used)
    if country == "AE":
        city, region, area = rng.choice(UAE_PLACES)
        free_zone = "Free Zone" in area
        legal = f"{stem} {cat.suffix_en} {'FZE' if free_zone else 'LLC'}"
        legal_ar: str | None = (
            f"{to_arabic(stem)} {cat.suffix_ar} {'م.م.ح' if free_zone else 'ذ.م.م'}"
        )
        prefix = {"Dubai": "DXB", "Abu Dhabi": "AUH", "Sharjah": "SHJ", "Ajman": "AJM"}.get(
            city, "RAK"
        )
        licence = f"{'JAFZ' if free_zone else prefix}-{rng.randint(1000000, 9999999)}"
        trn: str | None = make_trn(rng)
        tax_id = None
        address = Address(
            line1=f"{rng.choice(('Office', 'Unit', 'Shop', 'Warehouse'))} {rng.randint(3, 48)}"
            f"{rng.randint(1, 20):02d}, {rng.choice(STREET_WORDS)} {rng.choice(BUILDING_WORDS)}",
            line2=area,
            city=city,
            region=region,
            country="AE",
            po_box=str(rng.randint(10000, 499999)),
        )
        phone = uae_phone(rng)
    else:
        city, region = rng.choice(INTL_PLACES[country])
        suffix = INTL_SUFFIX[country]
        legal = f"{stem} {cat.suffix_en.replace(' Trading', '')} {suffix}"
        legal_ar = f"{to_arabic(stem)} {cat.suffix_ar}" if country in ("SA", "BH") else None
        licence = f"REG-{country}-{rng.randint(100000, 999999)}"
        trn = None
        tax_id = f"{country}{rng.randint(100000000, 999999999)}"
        address = Address(
            line1=f"{rng.randint(2, 180)} {rng.choice(STREET_WORDS)} Road",
            line2=f"Unit {rng.randint(1, 30)}",
            city=city,
            region=region,
            country=country,
            po_box=None,
        )
        phone = intl_phone(country, rng)
    domain = f"{stem.lower()}.example"
    domains = (domain, f"{stem.lower()}-billing.example") if rng.random() < 0.3 else (domain,)
    contacts = tuple(
        Contact(
            name=(name := person(rng)),
            role=role,
            email=f"{name.split()[0].lower()}.{name.split()[1].lower()}@{domain}",
            phone=uae_phone(rng) if country == "AE" else intl_phone(country, rng),
        )
        for role in rng.sample(ROLES, 2)
    )
    bank = pick_bank(rng, country)
    template = _template(i, country, category, rng)
    goods = cat.goods
    po_based = (
        goods or category in ("facilities", "security", "it_hardware") or (rng.random() < 0.35)
    )
    scale = (
        rng.choice(("small", "medium"))
        if category in ("office_supplies", "printing", "telecom", "catering")
        else rng.choice(("medium", "medium", "large", "small"))
    )
    since = dt.date(2023, 1, 1) + dt.timedelta(days=rng.randint(0, 900))
    return Vendor(
        id=f"VEN-{i + 1:03d}",
        legal_name=legal,
        legal_name_ar=legal_ar,
        trade_name=stem,
        country=country,
        trade_licence=licence,
        trn=trn,
        tax_id=tax_id,
        address=address,
        contacts=contacts,
        phone_on_file=phone,
        bank=bank_details(legal, bank, rng),
        email_domains=domains,
        sender_addresses=(f"billing@{domain}", f"accounts@{domain}"),
        category=category,
        template=template,
        po_based=po_based,
        since=since.isoformat(),
        typical_subtotal=SCALES[scale],
    )


def make_vendors(seed: int) -> list[Vendor]:
    used: set[str] = set()
    return [make_vendor(i, seed, used) for i in range(UAE_COUNT + len(INTL_COUNTRIES))]


def make_unknown_vendor(n: int, seed: int, used: set[str]) -> Vendor:
    """A vendor that is not in the master (first-time sender attacks)."""
    vendor = make_vendor(100 + n, seed, used, country="AE")
    return vendor.model_copy(update={"id": f"UNK-{n + 1:03d}", "since": "2026-09-20"})
