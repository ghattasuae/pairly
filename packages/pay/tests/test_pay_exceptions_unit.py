# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""The exceptions queue without a database (``pairly_pay.exceptions``): categories from
the stored checks and reasons, resolutions, SLA age, message templates, and the checks
on the personal paragraph and on the text a person sends."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from pairly_core.notifications.templating import has_em_dash
from pairly_pay.exceptions import (
    CATEGORIES,
    MESSAGE_TEMPLATES,
    RESOLUTIONS,
    age_of,
    categorise,
    check_message,
    check_note,
    compose,
    fill,
    note_inputs,
    template_for,
)
from pairly_pay.models import MessageRecipient, PayInvoice


def finding(code: str, *, review: bool = True, en: str = "Sentence.") -> dict[str, Any]:
    return {
        "code": code,
        "rule": code,
        "severity": "medium",
        "message": {"en": en, "ar": "جملة."},
        "needs_review": review,
    }


def invoice(checks: list[dict[str, Any]], reasons: list[str], **values: Any) -> PayInvoice:
    return PayInvoice(
        id=uuid.uuid4(),
        reference="AP-ABC123",
        status="needs_review",
        checks=checks,
        review_reasons=reasons,
        created_at=datetime.now(UTC) - timedelta(hours=values.pop("hours", 1)),
        **values,
    )


def keys(inv: PayInvoice) -> list[str]:
    return [c.key for c in categorise(inv)]


def test_matching_findings_have_their_own_categories() -> None:
    inv = invoice(
        [
            {
                "check": "matching",
                "status": "fail",
                "findings": [
                    finding("missing_receipt", en="No goods receipt."),
                    finding("price_variance"),
                    finding("total_variance"),
                ],
            }
        ],
        ["check:missing_receipt", "check:price_variance", "check:total_variance"],
    )
    found = categorise(inv)
    assert [c.key for c in found] == ["price_variance", "missing_receipt", "other"]
    assert found[1].explanations[0].en == "No goods receipt."
    assert found[1].explanations[0].ar == "جملة."


def test_other_checks_map_by_check_and_notes_are_ignored() -> None:
    inv = invoice(
        [
            {"check": "duplicates", "status": "fail", "findings": [finding("near_duplicate")]},
            {"check": "bank", "status": "fail", "findings": [finding("bank_mismatch")]},
            {"check": "policy", "status": "fail", "findings": [finding("policy_rule")]},
            {
                "check": "amounts",
                "status": "fail",
                "findings": [finding("round_amount", review=False)],
            },
            {"check": "forgery", "status": "error", "findings": []},
        ],
        [],
    )
    assert keys(inv) == ["duplicate_suspect", "bank_mismatch", "policy_breach", "other"]


def test_reading_reasons_are_low_confidence_and_nothing_is_never_empty() -> None:
    assert keys(invoice([], ["extraction:low_confidence:total"])) == ["low_confidence"]
    assert keys(invoice([], ["not_read"])) == ["low_confidence"]
    assert keys(invoice([], ["vendor_weak_match:name"])) == ["other"]
    assert keys(invoice([], ["risk:high"])) == ["other"]
    assert keys(invoice([], [])) == ["other"]


def test_every_category_has_a_resolution_and_no_em_dash() -> None:
    assert set(RESOLUTIONS) == set(CATEGORIES)
    for en, ar in RESOLUTIONS.values():
        assert not has_em_dash(en)
        assert not has_em_dash(ar)
    for t in MESSAGE_TEMPLATES.values():
        assert t.category in CATEGORIES
        assert not has_em_dash(t.en)
        assert not has_em_dash(t.ar)


def test_a_bank_mismatch_is_never_emailed() -> None:
    assert not [t for t in MESSAGE_TEMPLATES.values() if t.category == "bank_mismatch"]
    assert template_for("bank_mismatch", "delivery_note") is None
    assert template_for("missing_receipt", "delivery_note") is not None
    assert template_for("missing_receipt", "record_receipt") is not None
    assert MESSAGE_TEMPLATES["record_receipt"].recipient is MessageRecipient.REQUESTER


def test_templates_are_filled_by_plain_substitution() -> None:
    inv = invoice([], [], invoice_number="INV-77", po_number="PO-2026-0001", vendor_name="Z")
    text = fill(MESSAGE_TEMPLATES["delivery_note"], "en", inv, None)
    assert "invoice INV-77 for purchase order PO-2026-0001" in text
    no_number = invoice([], [], vendor_name="{po}")
    text = fill(MESSAGE_TEMPLATES["policy_query"], "en", no_number, None)
    assert "AP-ABC123" in text
    assert "{po}" in text  # a value is never filled again
    arabic = fill(MESSAGE_TEMPLATES["delivery_note"], "ar", no_number, None)
    assert "غير مذكور" in arabic


def test_age_and_overdue() -> None:
    old, fresh_invoice = invoice([], [], hours=50), invoice([], [], hours=2)
    now = datetime.now(UTC)
    assert age_of(old, 48, now).overdue is True
    fresh = age_of(fresh_invoice, 48, now)
    assert (fresh.hours, fresh.overdue) == (2, False)


@pytest.mark.parametrize(
    ("note", "locale", "problem"),
    [
        ("", "en", "empty"),
        ("Thanks — truly.", "en", "em_dash"),
        ("See https://example.test now.", "en", "link"),
        ("Write to ap@example.test please.", "en", "email_address"),
        ("Please pay within 30 days.", "en", "number"),
        ("ادفع خلال ٣٠ يوما.", "ar", "number"),
        ("شكرا لكم Pairly.", "ar", "latin_letters"),
        ("x" * 301, "en", "too_long"),
    ],
)
def test_the_personal_paragraph_is_checked(note: str, locale: str, problem: str) -> None:
    assert problem in check_note(note, locale)


def test_a_clean_note_passes_and_inputs_name_no_invoice() -> None:
    assert check_note("Thank you for your help with this.", "en") == []
    inputs = note_inputs(MESSAGE_TEMPLATES["po_missing"], "ar")
    assert set(inputs) == {"template", "recipient", "purpose", "language"}
    assert inputs["language"] == "ar"
    assert compose("Base.", None) == "Base."
    assert compose("Base.", "Note.") == "Base.\n\nNote."


def test_the_sent_text_is_checked() -> None:
    assert check_message("Hello.") == []
    assert "em_dash" in check_message("Hello — there.")
    assert "empty" in check_message("  ")
    assert "control_characters" in check_message("Hi\x07")
