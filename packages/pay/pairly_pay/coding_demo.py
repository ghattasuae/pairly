# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology

"""The demo chart of accounts and cost centres (synthetic), and the scripted model
coding of the demo invoice lines (``pairly_pay.ai_recordings``).

The demo seed imports both lists through the API as CSV (``gl_csv``, ``cc_csv``). The
scripted answers cover the lines of the three demo invoices of ``synth/pay`` so the
optional model suggestion answers offline in ``recorded`` mode. **They are scripted,
not model output.**
"""

from __future__ import annotations

from typing import Final

GL_ACCOUNTS: Final[tuple[tuple[str, str, str], ...]] = (
    ("6100", "Office supplies and stationery", "اللوازم المكتبية والقرطاسية"),
    ("6110", "Printing", "الطباعة"),
    ("6200", "Telecommunications", "الاتصالات"),
    ("6210", "Software subscriptions", "اشتراكات البرمجيات"),
    ("6300", "Repairs and maintenance", "الإصلاح والصيانة"),
    ("6310", "Building materials", "مواد البناء"),
    ("6400", "Professional services", "الخدمات المهنية"),
    ("6500", "Freight and logistics", "الشحن والخدمات اللوجستية"),
    ("6600", "Staff welfare and catering", "رعاية الموظفين والضيافة"),
    ("6700", "Marketing and events", "التسويق والفعاليات"),
    ("6800", "Security services", "خدمات الأمن"),
    ("1500", "IT equipment (capital)", "معدات تقنية المعلومات (رأسمالية)"),
)
COST_CENTRES: Final[tuple[tuple[str, str, str], ...]] = (
    ("CC-ADM", "Administration", "الإدارة"),
    ("CC-FAC", "Facilities", "المرافق"),
    ("CC-FIN", "Finance", "المالية"),
    ("CC-HR", "People", "الموارد البشرية"),
    ("CC-IT", "Information technology", "تقنية المعلومات"),
    ("CC-LOG", "Logistics", "الخدمات اللوجستية"),
    ("CC-MKT", "Marketing", "التسويق"),
    ("CC-OPS", "Operations", "العمليات"),
)
# description -> (GL account, cost centre, reason): the lines of the demo trio.
ANSWERS: Final[dict[str, tuple[str, str, str]]] = {
    "A4 copy paper, box of 5 reams": ("6100", "CC-ADM", "Copy paper is office stationery."),
    "Toner cartridge, black": ("6100", "CC-ADM", "Printer toner is an office supply."),
    "Whiteboard 120 x 90 cm": ("6100", "CC-ADM", "A whiteboard is office equipment of low value."),
    "Ballpoint pens, pack of 50": ("6100", "CC-ADM", "Pens are office stationery."),
    "Business fibre line 500 Mbps": ("6200", "CC-IT", "A fibre line is a telecom service."),
    "Router rental": ("6200", "CC-IT", "Router rental is part of the telecom service."),
    "SIP trunk channels": ("6200", "CC-IT", "SIP trunks are telephone lines."),
    "Gypsum board 12.5 mm": ("6310", "CC-FAC", "Gypsum board is a building material."),
    "Ceramic floor tiles": ("6310", "CC-FAC", "Floor tiles are building materials."),
    "LED panel light 60 x 60": ("6310", "CC-FAC", "Light panels are fitted in the building."),
    "Interior emulsion paint, 18 L": ("6310", "CC-FAC", "Paint is a building material."),
    "Steel door frame": ("6310", "CC-FAC", "A door frame is a building material."),
}


def _csv(rows: tuple[tuple[str, str, str], ...]) -> str:
    lines = ["code,name,name_ar"]
    lines += [f'{code},"{name}","{name_ar}"' for code, name, name_ar in rows]
    return "\n".join(lines) + "\n"


def gl_csv() -> str:
    return _csv(GL_ACCOUNTS)


def cc_csv() -> str:
    return _csv(COST_CENTRES)
