# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Bank payment files (WP4.7): CSV and ISO 20022 pain.001.001.09, pure.

The XML is checked structurally: namespace, element order of the group header and the
payment information block, transaction count and control sums, debtor and creditor
accounts, amounts with the currency's minor unit, and text escaping.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import xml.etree.ElementTree as ET
from decimal import Decimal

import pytest

from pairly_pay.bank_files import (
    CSV_HEADER,
    PAIN001_NS,
    Batch,
    Payment,
    amount_text,
    to_csv,
    to_pain001,
)

NS = {"p": PAIN001_NS}


def _payment(n: int, amount: str, **kw: object) -> Payment:
    values: dict[str, object] = {
        "end_to_end_id": f"PR-TEST-{n:04d}",
        "invoice_reference": f"AP-{n}",
        "invoice_number": f"INV-{n}",
        "vendor_reference": f"V-{n}",
        "beneficiary_name": f"Vendor {n} Trading LLC",
        "iban": "AE070331234567890123456",
        "bic": "EBILAEAD",
        "bank_name": "Synthetic Bank",
        "amount": Decimal(amount),
        "currency": "AED",
    }
    values.update(kw)
    return Payment(**values)  # type: ignore[arg-type]


def _batch(*payments: Payment, **kw: object) -> Batch:
    values: dict[str, object] = {
        "reference": "PR-TEST",
        "message_id": "PR-TEST-1",
        "created_at": dt.datetime(2026, 10, 14, 9, 30, 15, 123456, tzinfo=dt.UTC),
        "execution_date": dt.date(2026, 10, 20),
        "currency": "AED",
        "payer_name": "Acme Trading LLC",
        "payer_iban": "AE460090000000123456789",
        "payer_bic": "NBADAEAA",
        "payments": payments,
    }
    values.update(kw)
    return Batch(**values)  # type: ignore[arg-type]


def test_csv_rows_and_formula_escaping() -> None:
    batch = _batch(
        _payment(1, "1200.5"),
        _payment(2, "99.999", beneficiary_name='=HYPERLINK("x")', bic=None, bank_name=None),
    )
    rows = list(csv.reader(io.StringIO(to_csv(batch).decode("utf-8"))))
    assert tuple(rows[0]) == CSV_HEADER
    assert len(rows) == 3
    first = dict(zip(CSV_HEADER, rows[1], strict=True))
    assert first["amount"] == "1200.50"
    assert first["beneficiary_iban"] == "AE070331234567890123456"
    assert first["end_to_end_id"] == "PR-TEST-0001"
    assert first["execution_date"] == "2026-10-20"
    assert first["remittance"] == "Invoice INV-1 (AP-1)"
    second = dict(zip(CSV_HEADER, rows[2], strict=True))
    assert second["amount"] == "100.00"  # half up to the minor unit
    assert second["beneficiary_name"].startswith("'=")  # never a formula
    assert second["beneficiary_bic"] == ""


def _tag(element: ET.Element) -> str:
    return element.tag.split("}", 1)[1]


def test_pain001_structure_counts_and_sums() -> None:
    batch = _batch(
        _payment(1, "1200.50"),
        _payment(2, "300", iban="AE460090000000123456789", bic=None),
        _payment(3, "0.10", beneficiary_name="A & B <Supplies>"),
    )
    body = to_pain001(batch)
    assert body.startswith(b"<?xml")
    root = ET.fromstring(body)  # noqa: S314 (our own output)
    assert root.tag == f"{{{PAIN001_NS}}}Document"
    (init,) = list(root)
    assert _tag(init) == "CstmrCdtTrfInitn"
    header, info = list(init)
    assert [_tag(e) for e in header] == ["MsgId", "CreDtTm", "NbOfTxs", "CtrlSum", "InitgPty"]
    assert header.findtext("p:MsgId", namespaces=NS) == "PR-TEST-1"
    assert header.findtext("p:CreDtTm", namespaces=NS) == "2026-10-14T09:30:15+00:00"
    assert header.findtext("p:NbOfTxs", namespaces=NS) == "3"
    assert header.findtext("p:CtrlSum", namespaces=NS) == "1500.60"
    assert [_tag(e) for e in info][:10] == [
        "PmtInfId", "PmtMtd", "NbOfTxs", "CtrlSum", "ReqdExctnDt", "Dbtr", "DbtrAcct",
        "DbtrAgt", "ChrgBr", "CdtTrfTxInf",
    ]  # fmt: skip
    assert info.findtext("p:PmtMtd", namespaces=NS) == "TRF"
    assert info.findtext("p:CtrlSum", namespaces=NS) == "1500.60"
    assert info.findtext("p:ReqdExctnDt/p:Dt", namespaces=NS) == "2026-10-20"
    assert info.findtext("p:DbtrAcct/p:Id/p:IBAN", namespaces=NS) == "AE460090000000123456789"
    assert info.findtext("p:DbtrAgt/p:FinInstnId/p:BICFI", namespaces=NS) == "NBADAEAA"
    txs = info.findall("p:CdtTrfTxInf", NS)
    assert len(txs) == 3
    amounts = [tx.find("p:Amt/p:InstdAmt", NS) for tx in txs]
    assert [(a.text, a.get("Ccy")) for a in amounts if a is not None] == [
        ("1200.50", "AED"), ("300.00", "AED"), ("0.10", "AED"),
    ]  # fmt: skip
    assert [tx.findtext("p:PmtId/p:EndToEndId", namespaces=NS) for tx in txs] == [
        "PR-TEST-0001", "PR-TEST-0002", "PR-TEST-0003",
    ]  # fmt: skip
    assert txs[0].findtext("p:CdtrAgt/p:FinInstnId/p:BICFI", namespaces=NS) == "EBILAEAD"
    assert txs[1].find("p:CdtrAgt", NS) is None  # no BIC on file: no agent
    assert txs[1].findtext("p:CdtrAcct/p:Id/p:IBAN", namespaces=NS) == "AE460090000000123456789"
    assert txs[2].findtext("p:Cdtr/p:Nm", namespaces=NS) == "A & B <Supplies>"
    assert txs[0].findtext("p:RmtInf/p:Ustrd", namespaces=NS) == "Invoice INV-1 (AP-1)"
    for tx in txs:
        assert _tag(tx[0]) == "PmtId"


def test_pain001_needs_the_payer_and_one_currency() -> None:
    with pytest.raises(ValueError, match="payer_details_missing"):
        to_pain001(_batch(_payment(1, "1"), payer_iban=None))
    with pytest.raises(ValueError, match="mixed_currencies"):
        to_pain001(_batch(_payment(1, "1", currency="USD")))


def test_pain001_without_payer_bic_and_three_place_currency() -> None:
    body = to_pain001(
        _batch(_payment(1, "10.0005", currency="KWD"), currency="KWD", payer_bic=None)
    )
    root = ET.fromstring(body)  # noqa: S314 (our own output)
    info = root.find("p:CstmrCdtTrfInitn/p:PmtInf", NS)
    assert info is not None
    assert info.findtext("p:DbtrAgt/p:FinInstnId/p:Othr/p:Id", namespaces=NS) == "NOTPROVIDED"
    assert info.findtext("p:CtrlSum", namespaces=NS) == "10.001"
    assert amount_text(Decimal("2"), "KWD") == "2.000"
