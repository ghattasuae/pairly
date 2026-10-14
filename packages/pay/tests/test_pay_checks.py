# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Pay validation and fraud checks without a database (WP4.6).

True positives come from the synthetic attack invoices (``synth/pay``), negatives from
the regular invoices; the facts are the ground truth (what a perfect reader reads).
Plus the edges of each rule: rounding tolerance, tax codes, fuzzy numbers, lookalike
domains, the split window, fail-closed errors, the risk combination and the bilingual
explanations.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import replace
from decimal import Decimal
from functools import cache

import pytest

from pairly_core.guardrails.report import (
    Finding as GuardrailFinding,
)
from pairly_core.guardrails.report import (
    FindingType,
    GuardrailReport,
    text_digest,
)
from pairly_core.guardrails.report import (
    Severity as GuardrailSeverity,
)
from pairly_core.policy.rag import CheckOutcome, PolicyCheckResult
from pairly_core.risk.types import Severity
from pairly_pay.checks import risk
from pairly_pay.checks.amounts import check_amounts
from pairly_pay.checks.arithmetic import check_arithmetic
from pairly_pay.checks.bank import check_bank
from pairly_pay.checks.domains import decode, lookalike, skeleton
from pairly_pay.checks.duplicates import check_duplicates, lines_key, near_number, number_key
from pairly_pay.checks.forgery import check_forgery, fingerprint, numbering
from pairly_pay.checks.impersonation import check_impersonation, urgency_phrases
from pairly_pay.checks.injection import check_injection
from pairly_pay.checks.messages import MESSAGES, explain
from pairly_pay.checks.policy import rag_findings
from pairly_pay.checks.run import bundled_policy_check, run_pure_checks
from pairly_pay.checks.settings import CheckSettings, parse_settings
from pairly_pay.checks.types import (
    CheckResult,
    CheckStatus,
    DocumentFacts,
    History,
    InvoiceFacts,
    Line,
    PastInvoice,
    VendorFacts,
    run_safely,
)
from synth.pay.attacks import lookalike as synth_lookalike
from synth.pay.dataset import Dataset, DocSpec, build

SETTINGS = CheckSettings()
NOW = dt.datetime(2026, 10, 1, 9, tzinfo=dt.UTC)


@cache
def dataset() -> Dataset:
    return build()


def vendor_of(spec: DocSpec) -> VendorFacts | None:
    vid = spec.record.truth.vendor_id
    for v in dataset().vendors:
        if v.id == vid:
            return VendorFacts(
                id=v.id,
                name=v.legal_name,
                country=v.country,
                trn=v.trn,
                category=v.category,
                email_domains=tuple(v.email_domains),
                iban_on_file=v.bank.iban,
            )
    return None


def facts_of(spec: DocSpec, **changes: object) -> InvoiceFacts:
    t = spec.record.truth
    d = Decimal
    facts = InvoiceFacts(
        id=spec.record.id,
        received_at=NOW,
        vendor=vendor_of(spec),
        vendor_name=t.vendor_name,
        vendor_trn=t.vendor_trn,
        invoice_number=t.invoice_number,
        invoice_date=dt.date.fromisoformat(t.invoice_date),
        due_date=dt.date.fromisoformat(t.due_date),
        currency=t.currency,
        po_number=t.po_number,
        subtotal=d(t.subtotal),
        vat_rate=d(t.vat_rate),
        vat_amount=d(t.vat_amount),
        total=d(t.total),
        lines=tuple(
            Line(
                i,
                x.description,
                d(x.quantity),
                d(x.unit_price),
                d(x.vat_rate),
                d(x.vat_amount),
                d(x.line_total),
            )
            for i, x in enumerate(t.lines)
        ),
        bank_iban=t.bank.iban if t.bank else None,
        bank_account_name=t.bank.account_name if t.bank else None,
    )
    return replace(facts, **changes)  # type: ignore[arg-type]


def attacks(kind: str) -> list[DocSpec]:
    return [
        s for s in dataset().docs if s.record.attack is not None and kind in s.record.attack.types
    ]


def past_of(spec: DocSpec, **changes: object) -> PastInvoice:
    f = facts_of(spec)
    p = PastInvoice(
        id=spec.record.id,
        vendor_id=f.vendor.id if f.vendor else None,
        invoice_number=f.invoice_number,
        number_key=number_key(f.invoice_number),
        invoice_date=f.invoice_date,
        currency=f.currency,
        total=f.total,
        lines_key=lines_key(f.lines),
        sha256=None,
        fingerprint=None,
        received_at=NOW - dt.timedelta(days=30),
    )
    return replace(p, **changes)  # type: ignore[arg-type]


def codes(result: CheckResult) -> set[str]:
    return {f.code for f in result.findings}


def rules(result: CheckResult) -> set[str]:
    return {f.rule for f in result.findings}


# ------------------------------------------------------------------ arithmetic and VAT


def test_regular_invoices_pass_arithmetic_and_vat() -> None:
    failed = [
        (s.record.id, rules(r))
        for s in dataset().regular()
        if (r := check_arithmetic(facts_of(s), SETTINGS)).status is not CheckStatus.PASS
    ]
    assert failed == []


@pytest.mark.parametrize(
    "kind",
    [
        "vat_wrong_rate",
        "vat_on_zero_rated",
        "vat_missing",
        "vat_miscomputed",
        "line_total_error",
        "subtotal_error",
        "total_error",
        "line_vat_error",
        "editing_traces",
    ],
)
def test_tax_and_arithmetic_attacks_are_caught(kind: str) -> None:
    for spec in attacks(kind):
        assert spec.record.attack is not None
        expected = {
            c
            for c in spec.record.attack.expected_findings
            if c in {"arithmetic_error", "vat_error"}
        }
        found = codes(check_arithmetic(facts_of(spec), SETTINGS))
        assert expected <= found, (spec.record.id, expected, found)


def _simple(total: str, vat: str, line_total: str, rate: str = "5") -> InvoiceFacts:
    d = Decimal
    return InvoiceFacts(
        id="x",
        received_at=NOW,
        vendor=None,
        vendor_trn="100000000000003",
        currency="AED",
        subtotal=d(line_total),
        vat_amount=d(vat),
        total=d(total),
        lines=(Line(0, "Toner", d(2), d("50.005"), d(rate), d(vat), d(line_total)),),
    )


def test_rounding_tolerance_is_one_minor_unit() -> None:
    # 2 x 50.005 = 100.01; the vendor rounded the line to 100.00: within one fils.
    ok = check_arithmetic(_simple("105.00", "5.00", "100.00"), SETTINGS)
    assert ok.status is CheckStatus.PASS, rules(ok)
    off = check_arithmetic(_simple("105.02", "5.00", "100.00"), SETTINGS)
    assert "total_mismatch" in rules(off)
    strict = check_arithmetic(
        _simple("105.00", "5.00", "100.00"), replace(SETTINGS, rounding_units=0)
    )
    assert "line_total_mismatch" in rules(strict)


def test_reverse_charge_and_zero_rated_are_not_errors() -> None:
    foreign = VendorFacts("v", "V", "GB", None, "software", (), None)
    rc = replace(_simple("100.00", "0.00", "100.00", rate="0"), vendor=foreign)
    assert check_arithmetic(rc, SETTINGS).status is CheckStatus.PASS
    freight = replace(
        _simple("100.00", "0.00", "100.00", rate="0"),
        lines=(Line(0, "International sea freight", *(Decimal(v) for v in (1, 100, 0, 0, 100))),),
    )
    assert check_arithmetic(freight, SETTINGS).status is CheckStatus.PASS
    domestic_zero = _simple("100.00", "0.00", "100.00", rate="0")
    domestic_zero = replace(
        domestic_zero,
        lines=(replace(domestic_zero.lines[0], quantity=Decimal(1), unit_price=Decimal(100)),),
    )
    assert "zero_rate_unexplained" in rules(check_arithmetic(domestic_zero, SETTINGS))
    # A configured extra rate is allowed.
    ten = _simple("110.00", "10.00", "100.00", rate="10")
    ten = replace(ten, lines=(replace(ten.lines[0], quantity=Decimal(1), unit_price=Decimal(100)),))
    assert "rate_not_allowed" in rules(check_arithmetic(ten, SETTINGS))
    custom = replace(SETTINGS, extra_vat_rates=(Decimal(10),))
    assert check_arithmetic(ten, custom).status is CheckStatus.PASS


def test_arithmetic_fails_closed_without_values() -> None:
    assert (
        check_arithmetic(replace(_simple("1", "0", "1"), read=False), SETTINGS).status
        is CheckStatus.ERROR
    )
    assert (
        check_arithmetic(replace(_simple("1", "0", "1"), total=None), SETTINGS).status
        is CheckStatus.ERROR
    )


# ------------------------------------------------------------------ duplicates


def test_near_number_rules() -> None:
    assert near_number("INV202601239", "INV202601293") == "transposed_digits"
    assert near_number("RINV2026001", "INV2026001") == "prefix_or_suffix"
    assert near_number("INV2026001A", "INV2026001") == "prefix_or_suffix"
    assert near_number("2026001", "INV2026001") == "prefix_or_suffix"
    assert near_number("JUV01109", "JUV001109") == "zero_changed"
    # The vendor's next number is not a near copy.
    assert near_number("INV2026002", "INV2026001") is None
    assert number_key("inv-2026/001") == "INV2026001"


def test_duplicate_attacks_name_their_original() -> None:
    ds = dataset()
    for kind in ("exact_duplicate", "duplicate_resubmitted", "near_duplicate"):
        for spec in attacks(kind):
            assert spec.record.attack is not None
            original = ds.doc(spec.record.attack.original_id or "")
            same_file = spec.render() == original.render()
            history = History(
                vendor_invoices=(past_of(original),),
                same_file=(past_of(original),) if same_file else (),
            )
            facts = facts_of(spec)
            result = check_duplicates(
                facts, history, SETTINGS, own_lines_key=lines_key(facts.lines)
            )
            assert result.findings, spec.record.id
            assert {f.original_id for f in result.findings} == {original.record.id}


def test_regular_invoices_are_not_duplicates_of_each_other() -> None:
    by_vendor: dict[str, list[PastInvoice]] = {}
    flagged = []
    for spec in sorted(
        dataset().regular(), key=lambda s: (s.record.truth.invoice_date, s.record.id)
    ):
        facts = facts_of(spec)
        vid = facts.vendor.id if facts.vendor else ""
        history = History(vendor_invoices=tuple(by_vendor.get(vid, [])))
        if check_duplicates(
            facts, history, SETTINGS, own_lines_key=lines_key(facts.lines)
        ).findings:
            flagged.append(spec.record.id)
        by_vendor.setdefault(vid, []).insert(0, past_of(spec))
    assert flagged == []


def test_same_amount_window_is_configurable() -> None:
    spec = next(iter(dataset().regular()))
    facts = facts_of(spec, invoice_number="NEW-1", lines=())
    date = facts.invoice_date
    assert date is not None
    near = past_of(
        spec,
        id="p1",
        number_key="OTHER1",
        lines_key=None,
        invoice_date=date - dt.timedelta(days=10),
    )
    far = replace(near, invoice_date=date - dt.timedelta(days=30))
    assert rules(
        check_duplicates(facts, History(vendor_invoices=(near,)), SETTINGS, own_lines_key=None)
    ) == {"same_amount_window"}
    assert not check_duplicates(
        facts, History(vendor_invoices=(far,)), SETTINGS, own_lines_key=None
    ).findings
    wide = replace(SETTINGS, duplicate_window_days=40)
    assert check_duplicates(
        facts, History(vendor_invoices=(far,)), wide, own_lines_key=None
    ).findings


def test_same_file_in_another_document() -> None:
    spec = next(iter(dataset().regular()))
    result = check_duplicates(
        facts_of(spec), History(same_file_documents=("doc-1",)), SETTINGS, own_lines_key=None
    )
    assert rules(result) == {"same_file_other"}


# ------------------------------------------------------------------ bank


def test_bank_change_attacks_are_all_caught() -> None:
    caught = [
        s.record.id
        for s in attacks("bank_change")
        if "bank_mismatch" in rules(check_bank(facts_of(s)))
    ]
    assert len(caught) == len(attacks("bank_change")) == 5


def test_bank_rules() -> None:
    spec = next(s for s in dataset().regular() if s.record.truth.bank is not None)
    facts = facts_of(spec)
    assert check_bank(facts).status is CheckStatus.PASS
    assert facts.bank_iban is not None
    spaced = " ".join(facts.bank_iban[i : i + 4] for i in range(0, len(facts.bank_iban), 4))
    assert check_bank(replace(facts, bank_iban=spaced.lower())).status is CheckStatus.PASS
    assert check_bank(replace(facts, bank_iban=None)).status is CheckStatus.PASS
    assert rules(check_bank(replace(facts, bank_iban="AE000000000000000000001"))) == {
        "bank_iban_invalid"
    }
    assert rules(check_bank(replace(facts, vendor=None))) == {"bank_unverified"}
    assert facts.vendor is not None
    none_on_file = replace(facts, vendor=replace(facts.vendor, iban_on_file=None))
    found = check_bank(none_on_file).findings
    assert [f.rule for f in found] == ["bank_not_on_file"]
    assert found[0].severity is Severity.CRITICAL
    # Never a full IBAN in evidence.
    for f in check_bank(facts_of(attacks("bank_change")[0])).findings:
        assert all(
            len(str(v)) <= 36 and "AE" not in str(v)[:2]
            for k, v in f.evidence.items()
            if "last4" in k
        )


# ------------------------------------------------------------------ impersonation


@pytest.mark.parametrize("how", ["homoglyph", "extra_letter", "tld", "idn", "hyphen"])
def test_lookalike_domains(how: str) -> None:
    for vendor in dataset().vendors[:10]:
        known = vendor.email_domains[0]
        fake = synth_lookalike(known, how)
        found = lookalike(fake, [d for v in dataset().vendors for d in v.email_domains])
        assert found is not None, (fake, known)
        assert found.known == known, (fake, known)


def test_own_and_unrelated_domains_are_not_lookalikes() -> None:
    known = ["seltus.example", "zarqeth.example"]
    assert lookalike("seltus.example", known) is None
    assert lookalike("mail.seltus.example", known) is None
    assert lookalike("quorvane.example", known) is None
    assert decode("xn--sltus-bve.example")[1] is True
    assert skeleton("rn1") == skeleton("ml")


def test_impersonation_attacks() -> None:
    ds = dataset()
    domains = tuple((v.id, d) for v in ds.vendors for d in v.email_domains)
    for kind, expected in (
        ("lookalike_domain", {"sender_lookalike_domain", "sender_domain_mismatch"}),
        ("first_time_sender", {"first_time_sender", "unknown_vendor"}),
    ):
        for spec in attacks(kind):
            assert spec.record.attack is not None
            sender = spec.record.attack.notes["sender"].split("@")[1]
            facts = facts_of(spec, origin_domain=sender)
            found = codes(check_impersonation(facts, History(vendor_domains=domains)))
            assert expected <= found, (spec.record.id, found)
    # A known vendor writing from its own domain, seen before: nothing.
    spec = next(iter(ds.regular()))
    facts = facts_of(spec)
    assert facts.vendor is not None
    own = facts.vendor.email_domains[0]
    result = check_impersonation(
        replace(facts, origin_domain=own),
        History(vendor_domains=domains, seen_domains=frozenset({own})),
    )
    assert result.status is CheckStatus.PASS


def test_urgency_language_en_and_ar() -> None:
    assert "pay_today" in urgency_phrases(
        "URGENT: please pay today to avoid suspension of service."
    )
    assert "new_account" in urgency_phrases("Please note our new bank account details.")
    assert "urgent" in urgency_phrases("عاجل: يرجى السداد اليوم لتجنب إيقاف الخدمة.")
    assert "pay_today" in urgency_phrases("عاجل: يرجى السداد اليوم لتجنب إيقاف الخدمة.")
    # Visual order (reversed Arabic, as many PDFs store it).
    assert "urgent" in urgency_phrases("لجاع")
    assert urgency_phrases("Payment terms: Net 30. Thank you for your business.") == []


# ------------------------------------------------------------------ amounts


def test_inflated_round_and_split() -> None:
    ds = dataset()
    spec = next(iter(ds.regular()))
    facts = facts_of(spec, total=Decimal("100001.50"), subtotal=Decimal("95239.52"))
    history = History(
        vendor_invoices=tuple(
            past_of(spec, id=f"p{i}", total=Decimal(10000 + i * 300)) for i in range(6)
        )
    )
    found = check_amounts(facts, history, SETTINGS).findings
    assert [(f.rule, f.severity) for f in found] == [("amount_outlier", Severity.HIGH)]
    # Less than usual is not a risk.
    low = replace(facts, total=Decimal("100.00"), subtotal=Decimal("95.24"))
    assert not check_amounts(low, history, SETTINGS).findings
    # Round amounts.
    for kind in ("round_amount",):
        for s in attacks(kind):
            assert "round_amount" in rules(check_amounts(facts_of(s), History(), SETTINGS))
    # Split: three just under 50,000 within two days.
    group = attacks("split_invoice")[:3]
    pasts = tuple(past_of(s) for s in group[:2])
    split = check_amounts(facts_of(group[2]), History(vendor_invoices=pasts), SETTINGS)
    (f,) = [f for f in split.findings if f.rule == "split_under_threshold"]
    assert set(f.related_ids) == {s.record.id for s in group[:2]}
    assert f.needs_review
    narrow = replace(SETTINGS, split_window_days=0, approval_threshold=Decimal("200000"))
    assert not [
        f
        for f in check_amounts(facts_of(group[2]), History(vendor_invoices=pasts), narrow).findings
        if f.rule == "split_under_threshold"
    ]


def test_amounts_fail_closed() -> None:
    spec = next(iter(dataset().regular()))
    assert (
        check_amounts(facts_of(spec, total=None), History(), SETTINGS).status is CheckStatus.ERROR
    )


# ------------------------------------------------------------------ forgery


def _doc(**changes: object) -> DocumentFacts:
    base = DocumentFacts(
        media_type="application/pdf",
        sha256=None,
        metadata={
            "pdf:Creator": "Mizanto 4.3",
            "pdf:Producer": "ReportLab PDF Library",
            "pdf:CreationDate": "D:20260211093000+04'00'",
            "pdf:ModDate": "D:20260211093000+04'00'",
            "pdf:Title": "Invoice INV-2026-0001",
            "pdf:Author": "Seltus IT Solutions LLC",
        },
        text="",
        fonts=("Helvetica",),
        accents=("#2d2d2d",),
        page_size="595x842pt",
    )
    return replace(base, **changes)  # type: ignore[arg-type]


def test_template_mismatch_from_fingerprints() -> None:
    spec = next(iter(dataset().regular()))
    usual = fingerprint(_doc(), "INV-2026-0001")
    history = History(
        vendor_invoices=tuple(past_of(spec, id=f"p{i}", fingerprint=usual) for i in range(4))
    )
    same = facts_of(
        spec, invoice_number="INV-2026-0001", invoice_date=dt.date(2026, 2, 11), document=_doc()
    )
    assert not check_forgery(
        same, history, SETTINGS, own_fingerprint=fingerprint(_doc(), "INV-2026-0002")
    ).findings
    other = _doc(fonts=("Times",), accents=())
    result = check_forgery(same, history, SETTINGS, own_fingerprint=fingerprint(other, "#14728"))
    (f,) = [f for f in result.findings if f.code == "template_mismatch"]
    assert f.severity is Severity.HIGH
    assert numbering("INV-2026-01293") == "INV-9999-99999"
    # Too little history: not compared.
    short = History(vendor_invoices=history.vendor_invoices[:2])
    assert not check_forgery(
        same, short, SETTINGS, own_fingerprint=fingerprint(other, "#1")
    ).findings


def test_metadata_and_editing_traces_and_trn() -> None:
    spec = next(s for s in dataset().regular() if s.record.truth.vendor_trn)
    base = facts_of(spec, invoice_number="INV-2026-0001", invoice_date=dt.date(2026, 2, 11))
    meta = dict(_doc().metadata)
    meta |= {
        "pdf:CreationDate": "D:20260921180000+04'00'",
        "pdf:ModDate": "D:20260921090000+04'00'",
        "pdf:Creator": "Generic PDF editor 1.0",
        "pdf:Title": "Invoice INV-2026-0009",
        "pdf:Author": "admin",
    }
    doc = _doc(metadata=meta, overprinted=12)
    found = rules(
        check_forgery(replace(base, document=doc), History(), SETTINGS, own_fingerprint=None)
    )
    assert {
        "metadata_created_after_modified",
        "metadata_generic_tool",
        "metadata_created_late",
        "metadata_title_number",
        "metadata_author_generic",
        "editing_overprint",
    } <= found
    assert not check_forgery(
        replace(base, document=_doc()), History(), SETTINGS, own_fingerprint=None
    ).findings
    assert base.vendor_trn is not None
    assert rules(
        check_forgery(
            replace(base, vendor_trn=base.vendor_trn[:14]),
            History(),
            SETTINGS,
            own_fingerprint=None,
        )
    ) == {"trn_invalid"}
    other = str((int(base.vendor_trn) + 7) % 10**15).zfill(15)
    assert rules(
        check_forgery(replace(base, vendor_trn=other), History(), SETTINGS, own_fingerprint=None)
    ) == {"trn_mismatch"}


# ------------------------------------------------------------------ injection


def _report(*findings: GuardrailFinding, errors: tuple[str, ...] = ()) -> GuardrailReport:
    return GuardrailReport(
        findings=findings, errors=errors, text_sha256=text_digest(""), text_length=0
    )


def test_injection_findings_become_pay_findings() -> None:
    spec = next(iter(dataset().regular()))
    instruction = GuardrailFinding(
        id="f1", type=FindingType.INSTRUCTION, code="approval_manipulation",
        severity=GuardrailSeverity.HIGH, detector="instructions", evidence="approve this invoice",
    )  # fmt: skip
    hidden = GuardrailFinding(
        id="f2", type=FindingType.HIDDEN_TEXT, code="low_contrast",
        severity=GuardrailSeverity.MEDIUM, detector="hidden", evidence="x", remove=True,
    )  # fmt: skip
    result = check_injection(facts_of(spec, guardrail_report=_report(instruction, hidden)))
    assert [(f.code, f.severity) for f in result.findings] == [
        ("prompt_injection", Severity.CRITICAL),
        ("hidden_text", Severity.HIGH),
    ]
    # The flagged text itself never goes into a finding.
    assert all("approve" not in str(v) for f in result.findings for v in f.evidence.values())
    assert check_injection(facts_of(spec)).status is CheckStatus.ERROR
    assert (
        check_injection(facts_of(spec, guardrail_report=_report(errors=("hidden",)))).status
        is CheckStatus.ERROR
    )
    assert check_injection(facts_of(spec, guardrail_report=_report())).status is CheckStatus.PASS


# ------------------------------------------------------------------ policy, risk, messages


def test_policy_rules_and_no_policy_found() -> None:
    spec = next(iter(dataset().regular()))
    clean = bundled_policy_check(facts_of(spec), dt.date(2026, 10, 14))
    assert [f.code for f in clean.findings] == ["no_policy_found"]
    assert clean.status is CheckStatus.FAIL  # information, never a pass
    assert not clean.needs_review
    late = facts_of(spec, due_date=dt.date(2020, 1, 1))
    flagged = bundled_policy_check(late, dt.date(2026, 10, 14))
    assert flagged.needs_review
    assert any(f.evidence.get("rule_id") == "due_after_invoice_date" for f in flagged.findings)
    error, problem = rag_findings(PolicyCheckResult(CheckOutcome.ERROR, "down", "q"))
    assert error == []
    assert problem == "policy_check_error"


async def test_risk_combines_checks_and_fails_closed() -> None:
    spec = next(iter(dataset().regular()))
    facts = facts_of(spec, guardrail_report=_report())
    clean = run_pure_checks(facts, History(), SETTINGS, document_required=False)
    clean["policy"] = bundled_policy_check(facts, dt.date(2026, 10, 14))
    combined = await risk.score(clean)
    assert not combined.requires_human
    assert risk.explanation(combined)["en"].startswith("Risk is low")
    bad = run_pure_checks(
        facts_of(attacks("bank_change")[0], guardrail_report=_report()),
        History(),
        SETTINGS,
        document_required=False,
    )
    bad["policy"] = clean["policy"]
    high = await risk.score(bad)
    assert high.requires_human
    assert high.level is Severity.CRITICAL
    assert set(risk.explanation(high)) == {"en", "ar"}
    broken = dict(clean)
    broken["amounts"] = run_safely("amounts", "1", lambda: 1 / 0)  # type: ignore[arg-type,return-value]
    assert broken["amounts"].status is CheckStatus.ERROR
    assert (await risk.score(broken)).requires_human
    missing = {k: v for k, v in clean.items() if k != "forgery"}
    assert (await risk.score(missing)).requires_human


def test_every_message_has_both_languages_and_no_em_dash() -> None:
    for rule, (en, ar) in MESSAGES.items():
        assert en, rule
        assert ar, rule
        assert chr(0x2014) not in en + ar, rule
        assert chr(0x2013) not in en + ar, rule
    en, ar = explain("same_number", {"number": "INV-1", "original": "2026-01-01"})
    assert "INV-1" in en
    assert "INV-1" in ar
    assert "?" in explain("same_number", {})[0]


def test_settings_parse_with_defaults() -> None:
    parsed = parse_settings({"standard_vat_rate": "15", "duplicate_window_days": 30, "junk": 1})
    assert parsed.standard_vat_rate == Decimal(15)
    assert parsed.duplicate_window_days == 30
    assert parse_settings({"standard_vat_rate": "-1", "split_window_days": "x"}) == CheckSettings()
    assert parse_settings(None) == CheckSettings()
    assert parse_settings(CheckSettings().as_json()) == CheckSettings()
