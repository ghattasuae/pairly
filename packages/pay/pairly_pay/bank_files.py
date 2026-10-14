# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Bank payment files of a payment run (brief 4.6.3): CSV and ISO 20022 pain.001.

Pure functions, no I/O and no external libraries: the XML is built with the standard
library's ``xml.etree.ElementTree`` (which escapes text). Nothing here sends anything
anywhere; the person downloads the file and takes it to their bank themselves.

* ``to_csv``: one row per payment, with a header. Cells that a spreadsheet would read
  as a formula (``=``, ``+``, ``-``, ``@``, tab, carriage return) get a leading ``'``.
* ``to_pain001``: a ``pain.001.001.09`` customer credit transfer initiation with one
  payment information block (one debtor account, one execution date, one currency) and
  one credit transfer per invoice. Control sums are exact decimals.

Amounts are written with the currency's minor unit (two places; three for BHD, KWD,
OMR, JOD, TND, LYD and IQD), rounded half up.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import re
import xml.etree.ElementTree as ET
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from pairly_pay.checks.settings import to_minor_unit

PAIN001_NS: Final = "urn:iso:std:iso:20022:tech:xsd:pain.001.001.09"
CSV_HEADER: Final = (
    "payment_run",
    "execution_date",
    "item",
    "end_to_end_id",
    "invoice_reference",
    "invoice_number",
    "vendor_reference",
    "beneficiary_name",
    "beneficiary_iban",
    "beneficiary_bic",
    "beneficiary_bank",
    "amount",
    "currency",
    "remittance",
)
_CONTROL: Final = re.compile(r"[\x00-\x1f\x7f]")
_FORMULA: Final = ("=", "+", "-", "@", "\t", "\r")
MAX_NAME: Final = 140
MAX_ID: Final = 35


@dataclass(frozen=True, slots=True)
class Payment:
    end_to_end_id: str
    invoice_reference: str
    invoice_number: str | None
    vendor_reference: str
    beneficiary_name: str
    iban: str
    bic: str | None
    bank_name: str | None
    amount: Decimal
    currency: str


@dataclass(frozen=True, slots=True)
class Batch:
    reference: str
    message_id: str
    created_at: dt.datetime
    execution_date: dt.date
    currency: str
    payer_name: str | None
    payer_iban: str | None
    payer_bic: str | None
    payments: Sequence[Payment]


def amount_text(value: Decimal, currency: str) -> str:
    return format(to_minor_unit(value, currency), "f")


def control_sum(payments: Sequence[Payment]) -> Decimal:
    return sum((to_minor_unit(p.amount, p.currency) for p in payments), Decimal(0))


def clean(text: str | None, limit: int = MAX_NAME) -> str:
    return _CONTROL.sub(" ", text or "").strip()[:limit]


def _cell(text: str) -> str:
    return "'" + text if text.startswith(_FORMULA) else text


def remittance(payment: Payment) -> str:
    number = payment.invoice_number or payment.invoice_reference
    return clean(f"Invoice {number} ({payment.invoice_reference})")


def to_csv(batch: Batch) -> bytes:
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\r\n")
    writer.writerow(CSV_HEADER)
    for index, p in enumerate(batch.payments, start=1):
        row = (
            batch.reference,
            batch.execution_date.isoformat(),
            str(index),
            p.end_to_end_id,
            p.invoice_reference,
            p.invoice_number or "",
            p.vendor_reference,
            clean(p.beneficiary_name),
            p.iban,
            p.bic or "",
            clean(p.bank_name),
            amount_text(p.amount, p.currency),
            p.currency,
            remittance(p),
        )
        writer.writerow([_cell(clean(v, 400)) for v in row])
    return out.getvalue().encode("utf-8")


def _sub(parent: ET.Element, tag: str, text: str | None = None, **attrs: str) -> ET.Element:
    element = ET.SubElement(parent, f"{{{PAIN001_NS}}}{tag}", attrs)
    if text is not None:
        element.text = text
    return element


def _agent(parent: ET.Element, tag: str, bic: str | None) -> None:
    institution = _sub(_sub(parent, tag), "FinInstnId")
    if bic:
        _sub(institution, "BICFI", bic)
    else:
        _sub(_sub(institution, "Othr"), "Id", "NOTPROVIDED")


def to_pain001(batch: Batch) -> bytes:
    """``pain.001.001.09``. Raises ``ValueError`` without the payer's name and IBAN."""
    if not batch.payer_name or not batch.payer_iban:
        raise ValueError("payer_details_missing")
    if any(p.currency != batch.currency for p in batch.payments):
        raise ValueError("mixed_currencies")
    ET.register_namespace("", PAIN001_NS)
    count = str(len(batch.payments))
    total = amount_text(control_sum(batch.payments), batch.currency)
    root = ET.Element(f"{{{PAIN001_NS}}}Document")
    initiation = _sub(root, "CstmrCdtTrfInitn")
    header = _sub(initiation, "GrpHdr")
    _sub(header, "MsgId", clean(batch.message_id, MAX_ID))
    _sub(header, "CreDtTm", batch.created_at.replace(microsecond=0).isoformat())
    _sub(header, "NbOfTxs", count)
    _sub(header, "CtrlSum", total)
    _sub(_sub(header, "InitgPty"), "Nm", clean(batch.payer_name))

    info = _sub(initiation, "PmtInf")
    _sub(info, "PmtInfId", clean(batch.reference, MAX_ID))
    _sub(info, "PmtMtd", "TRF")
    _sub(info, "NbOfTxs", count)
    _sub(info, "CtrlSum", total)
    _sub(_sub(info, "ReqdExctnDt"), "Dt", batch.execution_date.isoformat())
    _sub(_sub(info, "Dbtr"), "Nm", clean(batch.payer_name))
    _sub(_sub(_sub(info, "DbtrAcct"), "Id"), "IBAN", batch.payer_iban)
    _agent(info, "DbtrAgt", batch.payer_bic)
    _sub(info, "ChrgBr", "SHAR")
    for index, p in enumerate(batch.payments, start=1):
        tx = _sub(info, "CdtTrfTxInf")
        ids = _sub(tx, "PmtId")
        _sub(ids, "InstrId", clean(f"{batch.reference}-{index}", MAX_ID))
        _sub(ids, "EndToEndId", clean(p.end_to_end_id, MAX_ID))
        _sub(_sub(tx, "Amt"), "InstdAmt", amount_text(p.amount, p.currency), Ccy=p.currency)
        if p.bic:
            _agent(tx, "CdtrAgt", p.bic)
        _sub(_sub(tx, "Cdtr"), "Nm", clean(p.beneficiary_name))
        _sub(_sub(_sub(tx, "CdtrAcct"), "Id"), "IBAN", p.iban)
        _sub(_sub(tx, "RmtInf"), "Ustrd", remittance(p))
    body: bytes = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    return body
