# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Offers (M3) without a database: compensation rules, the approval rules, the payload
binding and the letter in English and Arabic."""

from __future__ import annotations

import io
import unicodedata
import uuid
from datetime import date

import pdfplumber
import pytest

from pairly_core.approvals.rules import parse_rules
from pairly_core.errors import BadRequestError
from pairly_core.notifications.templating import has_em_dash
from pairly_hire.offers import pdf, service
from pairly_hire.offers.graph import APPROVAL_RULES_V1
from pairly_hire.offers.models import HireOffer
from pairly_hire.offers.templates import BY_KEY, TEMPLATES

COMP = {"base_monthly": "20000", "housing_monthly": "5000", "transport_monthly": "1000.5"}


def test_compensation_is_canonical_and_checked() -> None:
    clean = service.clean_compensation(COMP)
    assert clean == {
        "currency": "AED",
        "base_monthly": "20000.00",
        "housing_monthly": "5000.00",
        "transport_monthly": "1000.50",
        "other_monthly": "0.00",
        "bonus_target_pct": "0.00",
        "annual_total": "312006.00",
    }
    for bad in (
        {"base_monthly": "0"},
        {"base_monthly": "-1"},
        {"base_monthly": "abc"},
        {"base_monthly": "20000", "currency": "dollars"},
        {"base_monthly": "20000", "bonus_target_pct": "120"},
        {"base_monthly": "99999999"},
    ):
        with pytest.raises(BadRequestError):
            service.clean_compensation(bad)
    with pytest.raises(BadRequestError):
        service.clean_conditions(["x" * 301])
    assert service.clean_conditions(["  a   b ", ""]) == ["a b"]


def test_rules_need_the_hiring_manager_then_hr_and_finance_above_the_threshold() -> None:
    rules = parse_rules(APPROVAL_RULES_V1)
    assert rules.approval_type == "hire.offer"
    small = rules.select_chain({"annual_total": "600000.00"})
    assert [s.name for s in small.steps] == ["hiring_manager", "hr"]
    large = rules.select_chain({"annual_total": "600000.01"})
    assert [s.name for s in large.steps] == ["hiring_manager", "hr", "finance"]
    assert rules.select_chain({}).name == "above_threshold"  # unknown total: the strict chain


def _offer(**extra: object) -> HireOffer:
    row = HireOffer(
        id=uuid.uuid4(),
        org_id=uuid.uuid4(),
        application_id=uuid.uuid4(),
        candidate_id=uuid.uuid4(),
        job_id=uuid.uuid4(),
        reference="OFR-ABC234",
        template_key="standard",
        locale="en",
        job_title="Backend Engineer",
        start_date=date(2026, 12, 1),
        conditions=["Probation period of 6 months."],
        status="draft",
        version=1,
        created_by=uuid.uuid4(),
    )
    row.compensation = service._seal(row, "compensation", service.clean_compensation(COMP))
    for k, v in extra.items():
        setattr(row, k, v)
    return row


def test_payload_binds_compensation_and_conditions_by_hash() -> None:
    row = _offer()
    payload = service.payload_of(row)
    assert payload["annual_total"] == "312006.00"
    assert "base_monthly" not in payload
    row.conditions = ["Probation period of 3 months."]
    assert service.payload_of(row)["conditions_hash"] != payload["conditions_hash"]
    row.compensation = service._seal(
        row, "compensation", service.clean_compensation({**COMP, "base_monthly": "20001"})
    )
    assert service.payload_of(row)["compensation_hash"] != payload["compensation_hash"]


def test_templates_are_bilingual_without_em_dashes() -> None:
    assert set(BY_KEY) == {"standard", "fixed_term"}
    for t in TEMPLATES:
        assert len(t.conditions) == len(t.conditions_ar)
        text = " ".join([t.opening, t.opening_ar, t.closing, t.closing_ar, *t.conditions])
        assert not has_em_dash(text + " ".join(t.conditions_ar))


@pytest.mark.parametrize("locale", ["en", "ar"])
def test_letter_pdf_text_extracts_in_both_languages(locale: str) -> None:
    template = BY_KEY["standard"]
    data = service.LetterData(
        company="Acme Logistics",
        candidate_name="Layla Haddad",
        reference="OFR-ABC234",
        job_title="Backend Engineer",
        start_date=date(2026, 12, 1),
        compensation=service.clean_compensation({**COMP, "bonus_target_pct": "10"}),
        conditions=list(template.conditions_ar if locale == "ar" else template.conditions),
        template=template,
        locale=locale,
    )
    blob = pdf.render_offer_pdf(data)
    assert blob.startswith(b"%PDF")
    with pdfplumber.open(io.BytesIO(blob)) as doc:
        text = unicodedata.normalize("NFKC", "\n".join(p.extract_text() or "" for p in doc.pages))
    assert "Layla Haddad" in text
    assert "312,006.00" in text
    assert "2026-12-01" in text
    if locale == "en":
        assert "Offer of employment" in text
        assert "Bonus target: 10.00%" in text
    else:
        assert "عرض عمل"[::-1] in text
        assert "الشروط"[::-1] in text
