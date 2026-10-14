# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Run the Pay checks on the synthetic dataset and measure them (see ``__init__``)."""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import hashlib
import json
import tempfile
import time
from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

from evals.pay.matching import PoBook, evaluate
from pairly_core.documents.docx import DocxParser
from pairly_core.documents.model import NormalizedDocument
from pairly_core.documents.ocr import ImageParser
from pairly_core.documents.pdf import PdfParser
from pairly_core.documents.registry import ParseOptions
from pairly_core.documents.ubl import UblInvoiceParser
from pairly_core.guardrails import ScanContext, scan_document
from pairly_core.guardrails.report import GuardrailReport
from pairly_pay.checks import matching, risk
from pairly_pay.checks.document import document_facts
from pairly_pay.checks.run import bundled_policy_check, keys_of, run_pure_checks
from pairly_pay.checks.settings import CheckSettings
from pairly_pay.checks.types import (
    CheckResult,
    CheckStatus,
    History,
    InvoiceFacts,
    Line,
    PastInvoice,
    VendorFacts,
)
from synth.pay.dataset import Dataset, DocSpec, build
from synth.pay.models import EmailRecord, Vendor

RESULTS_DIR: Final = Path(__file__).resolve().parents[1] / "results"
RESULTS: Final = RESULTS_DIR / "pay.json"
REPORT: Final = Path(__file__).resolve().parents[1] / "report.md"
OPTIONS: Final = ParseOptions(ocr_languages="eng+ara")
SCANS: Final = frozenset({"png", "jpg", "scan_pdf"})
AS_OF: Final = dt.date(2026, 10, 14)
DUPLICATE_CODES: Final = frozenset(
    {"duplicate_file_hash", "duplicate_invoice_number", "near_duplicate"}
)
INJECTION_CODES: Final = frozenset({"prompt_injection", "hidden_text", "metadata_injection"})
MONEY_CODES: Final = frozenset({"arithmetic_error", "vat_error"})
FLAG_SEVERITIES: Final = frozenset({"medium", "high", "critical"})


@dataclass
class Case:
    id: str
    kind: str
    types: tuple[str, ...]
    expected: tuple[str, ...]
    original_id: str | None
    parsed: bool
    codes: set[str] = field(default_factory=set)
    flagged_codes: set[str] = field(default_factory=set)
    originals: set[str] = field(default_factory=set)
    errors: list[str] = field(default_factory=list)
    review: bool = False
    risk_level: str = "low"
    related_flag: bool = False


def _d(value: str | None) -> Decimal | None:
    return Decimal(value) if value is not None else None


def _domain(address: str | None) -> str | None:
    if not address or "@" not in address:
        return None
    return address.rsplit("@", 1)[1].strip().lower() or None


def _vendor(v: Vendor) -> VendorFacts:
    return VendorFacts(
        id=v.id,
        name=v.legal_name,
        country=v.country,
        trn=v.trn,
        category=v.category,
        email_domains=tuple(d.lower() for d in v.email_domains),
        iban_on_file=v.bank.iban,
    )


def _origin(spec: DocSpec, emails: dict[str, EmailRecord]) -> str | None:
    record = spec.record
    email = emails.get(record.email_id or "")
    if email is not None:
        return _domain(email.original_sender) or _domain(email.sender)
    if record.attack is not None:
        return _domain(record.attack.notes.get("sender"))
    return None


def _parse(spec: DocSpec, data: bytes, work: Path, ds: Dataset) -> NormalizedDocument | None:
    fmt = spec.record.format
    if fmt == "email_body":
        from synth.pay.recordings import materialise

        return materialise(spec, ds).doc
    path = work / Path(spec.record.file).name
    path.write_bytes(data)
    if fmt == "pdf":
        return PdfParser().parse(path, OPTIONS)
    if fmt == "docx":
        return DocxParser().parse(path, OPTIONS)
    if fmt == "xml":
        return UblInvoiceParser().parse(path, OPTIONS)
    if fmt in ("png", "jpg"):
        return ImageParser().parse(path, OPTIONS)
    if fmt == "scan_pdf":
        return PdfParser().parse(path, OPTIONS)
    return None


def _facts(
    spec: DocSpec,
    vendors: dict[str, VendorFacts],
    origin: str | None,
    doc: NormalizedDocument | None,
    sha: str,
    report: GuardrailReport | None,
) -> InvoiceFacts:
    t = spec.record.truth
    date = dt.date.fromisoformat(t.invoice_date)
    return InvoiceFacts(
        id=spec.record.id,
        received_at=dt.datetime.combine(date, dt.time(9), dt.UTC),
        vendor=vendors.get(t.vendor_id or ""),
        vendor_name=t.vendor_name,
        vendor_trn=t.vendor_trn,
        invoice_number=t.invoice_number,
        invoice_date=date,
        due_date=dt.date.fromisoformat(t.due_date),
        currency=t.currency,
        po_number=t.po_number,
        subtotal=_d(t.subtotal),
        vat_rate=_d(t.vat_rate),
        vat_amount=_d(t.vat_amount),
        total=_d(t.total),
        lines=tuple(
            Line(
                index=i,
                description=x.description,
                quantity=_d(x.quantity),
                unit_price=_d(x.unit_price),
                vat_rate=_d(x.vat_rate),
                vat_amount=_d(x.vat_amount),
                line_total=_d(x.line_total),
            )
            for i, x in enumerate(t.lines)
        ),
        bank_iban=t.bank.iban if t.bank else None,
        bank_account_name=t.bank.account_name if t.bank else None,
        origin_domain=origin,
        document=document_facts(doc, sha) if doc is not None else None,
        guardrail_report=report,
    )


def _ratio(num: int, den: int) -> float | None:
    return round(num / den, 4) if den else None


def _order(spec: DocSpec) -> tuple[str, int, str]:
    return (
        spec.record.truth.invoice_date,
        0 if spec.record.kind == "invoice" else 1,
        spec.record.id,
    )


async def _run(count: int, seed: int, include_scans: bool, limit: int | None) -> dict[str, Any]:
    started = time.monotonic()
    ds = build(count, seed)
    settings = CheckSettings()
    vendors = {v.id: _vendor(v) for v in ds.vendors}
    vendor_domains = tuple((v.id, d.lower()) for v in ds.vendors for d in v.email_domains)
    emails = {e.id: e for e in ds.emails}
    specs = sorted(ds.docs, key=_order)[: limit or None]
    # The synthetic POs and receipts on file: matching runs with them like in production
    # (its own metrics are measured by ``evals.pay.matching``).
    book = PoBook(ds)
    past: list[tuple[str | None, PastInvoice]] = []
    seen: set[str] = set()
    cases: dict[str, Case] = {}
    parse_errors: list[str] = []
    with tempfile.TemporaryDirectory(prefix="pairly-eval-pay-") as tmp:
        work = Path(tmp)
        for spec in specs:
            record = spec.record
            data = spec.render()
            sha = hashlib.sha256(data).hexdigest()
            doc: NormalizedDocument | None = None
            report: GuardrailReport | None = None
            if record.format not in SCANS or include_scans:
                try:
                    doc = _parse(spec, data, work, ds)
                except Exception as exc:  # recorded, never hidden
                    parse_errors.append(f"{record.id}:{type(exc).__name__}")
                if doc is not None:
                    report = scan_document(
                        doc, context=ScanContext(filename=Path(record.file).name)
                    )
            origin = _origin(spec, emails)
            facts = _facts(spec, vendors, origin, doc, sha, report)
            vendor_id = facts.vendor.id if facts.vendor else None
            history = book.history(
                facts,
                History(
                    vendor_invoices=tuple(
                        p for vid, p in reversed(past) if vendor_id is not None and vid == vendor_id
                    ),
                    same_file=tuple(p for _, p in past if p.sha256 == sha),
                    vendor_domains=vendor_domains,
                    seen_domains=frozenset(seen),
                ),
            )
            keys = keys_of(facts)
            results: dict[str, CheckResult] = run_pure_checks(
                facts, history, settings, keys=keys, document_required=False
            )
            if report is None:
                # Scans not parsed here: the guardrails did not run (not a check error).
                results.pop("injection")
            results["policy"] = bundled_policy_check(facts, AS_OF)
            if record.kind == "invoice":
                # A rejected attack takes nothing from its PO; a regular invoice does.
                matched = matching.match_invoice(facts, history, settings)
                book.take(matched.po_id, matched.allocations)
            # The fraud metrics below leave matching out: a price or quantity variance on
            # a clean invoice is an expected exception, not a false positive. Matching is
            # measured on its own cases (``result["matching"]``).
            fraud = {k: v for k, v in results.items() if k != "matching"}
            combined = await risk.score(
                fraud
                | {"matching": CheckResult("matching", "1", CheckStatus.PASS)}
                | (
                    {}
                    if "injection" in results
                    else {"injection": CheckResult("injection", "1", CheckStatus.PASS)}
                )
            )
            case = Case(
                id=record.id,
                kind=record.kind,
                types=record.attack.types if record.attack else (),
                expected=record.attack.expected_findings if record.attack else (),
                original_id=record.attack.original_id if record.attack else None,
                parsed=doc is not None,
                risk_level=combined.level.value,
            )
            for name, outcome in results.items():
                if outcome.status is CheckStatus.ERROR:
                    case.errors.append(f"{name}:{outcome.error}")
                if name == "matching":
                    continue
                for f in outcome.findings:
                    case.codes.add(f.code)
                    if f.severity.value in FLAG_SEVERITIES:
                        case.flagged_codes.add(f.code)
                    if f.original_id:
                        case.originals.add(f.original_id)
                    for related in f.related_ids:
                        if related in cases:
                            cases[related].codes.add(f.code)
                            cases[related].flagged_codes.add(f.code)
                            cases[related].review = True
                            cases[related].related_flag = True
            case.review = any(r.needs_review for r in fraud.values()) or combined.requires_human
            cases[record.id] = case
            past.append(
                (
                    vendor_id,
                    PastInvoice(
                        id=record.id,
                        vendor_id=vendor_id,
                        invoice_number=facts.invoice_number,
                        number_key=keys.number_key,
                        invoice_date=facts.invoice_date,
                        currency=facts.currency,
                        total=facts.total,
                        lines_key=keys.lines_key,
                        sha256=sha,
                        fingerprint=keys.fingerprint,
                        received_at=facts.received_at,
                    ),
                )
            )
            if origin:
                seen.add(origin)
    result = metrics(list(cases.values()))
    result["matching"] = evaluate(ds, settings)
    result["dataset"] = {
        "count": count,
        "seed": seed,
        "documents": len(cases),
        "regular": sum(1 for c in cases.values() if c.kind == "invoice"),
        "attacks": sum(1 for c in cases.values() if c.kind == "attack"),
        "parsed": sum(1 for c in cases.values() if c.parsed),
        "scans_included": include_scans,
        "parse_errors": parse_errors,
        "values": "ground truth (what a perfect extractor reads)",
    }
    result["seconds"] = round(time.monotonic() - started, 1)
    return result


def _recall(cases: list[Case], expect: frozenset[str], found: frozenset[str]) -> dict[str, Any]:
    positives = [c for c in cases if c.kind == "attack" and expect & set(c.expected)]
    hit = [c for c in positives if found & c.codes]
    return {
        "positives": len(positives),
        "detected": len(hit),
        "recall": _ratio(len(hit), len(positives)),
        "missed": sorted(c.id for c in positives if c not in hit),
    }


def metrics(cases: list[Case]) -> dict[str, Any]:
    attacks = [c for c in cases if c.kind == "attack"]
    clean = [c for c in cases if c.kind == "invoice"]

    dup = _recall(cases, DUPLICATE_CODES, DUPLICATE_CODES)
    flagged_dup = [c for c in cases if DUPLICATE_CODES & c.codes]
    true_dup = [c for c in flagged_dup if c.kind == "attack" and DUPLICATE_CODES & set(c.expected)]
    with_original = [
        c for c in true_dup if c.original_id is not None and c.original_id in c.originals
    ]
    dup |= {
        "flagged": len(flagged_dup),
        "precision": _ratio(len(true_dup), len(flagged_dup)),
        "false_positives": sorted(c.id for c in flagged_dup if c not in true_dup),
        "original_identified_rate": _ratio(
            len(with_original), sum(1 for c in true_dup if c.original_id)
        ),
    }

    bank = _recall(cases, frozenset({"bank_mismatch"}), frozenset({"bank_mismatch"}))
    bank["clean_false_positives"] = sorted(c.id for c in clean if "bank_mismatch" in c.codes)
    lookalike = _recall(
        cases, frozenset({"sender_lookalike_domain"}), frozenset({"sender_lookalike_domain"})
    )
    lookalike["false_positives"] = sorted(
        c.id
        for c in cases
        if "sender_lookalike_domain" in c.codes and "sender_lookalike_domain" not in c.expected
    )
    money = _recall(cases, MONEY_CODES, MONEY_CODES)
    money_pos = [c for c in attacks if MONEY_CODES & set(c.expected)]
    money["type_level_recall"] = _ratio(
        sum(1 for c in money_pos if (MONEY_CODES & set(c.expected)) <= c.codes), len(money_pos)
    )
    money["clean_false_positives"] = sorted(c.id for c in clean if MONEY_CODES & c.codes)

    inj_pos = [c for c in attacks if "prompt_injection" in c.expected]
    inj_hit = [c for c in inj_pos if INJECTION_CODES & c.flagged_codes]
    injection = {
        "positives": len(inj_pos),
        "caught": len(inj_hit),
        "catch_rate": _ratio(len(inj_hit), len(inj_pos)),
        "missed": sorted(c.id for c in inj_pos if c not in inj_hit),
        "clean_flagged": sorted(c.id for c in clean if INJECTION_CODES & c.flagged_codes),
    }

    clean_review = [c for c in clean if c.review]
    clean_flag = [c for c in clean if c.flagged_codes]
    fp_codes = Counter(code for c in clean for code in c.flagged_codes)
    false_positives = {
        "clean_invoices": len(clean),
        "routed_to_review": len(clean_review),
        "review_rate": _ratio(len(clean_review), len(clean)),
        "flagged_medium_or_worse": len(clean_flag),
        "flag_rate": _ratio(len(clean_flag), len(clean)),
        "by_code": dict(sorted(fp_codes.items())),
        "examples": sorted(c.id for c in clean_review)[:40],
    }

    expected = Counter(code for c in attacks for code in c.expected)
    per_finding = {
        code: {
            "expected": n,
            "found": sum(1 for c in attacks if code in c.expected and code in c.codes),
            "recall": _ratio(sum(1 for c in attacks if code in c.expected and code in c.codes), n),
        }
        for code, n in sorted(expected.items())
    }
    errors = Counter(e.split(":", 1)[0] for c in cases for e in c.errors)
    return {
        "duplicates": dup,
        "bank_mismatch": bank,
        "lookalike_domain": lookalike,
        "arithmetic_vat": money,
        "injection": injection,
        "false_positives": false_positives,
        "attacks_routed_to_review": _ratio(sum(1 for c in attacks if c.review), len(attacks)),
        "per_expected_finding": per_finding,
        "check_errors": sum(errors.values()),
        "check_errors_by_check": dict(sorted(errors.items())),
        "check_error_cases": sorted(f"{c.id}:{e}" for c in cases for e in c.errors)[:40],
    }


def main(argv: list[str] | None = None) -> int:
    from evals.pay.report import write_report

    parser = argparse.ArgumentParser(prog="python -m evals.pay")
    parser.add_argument("--count", type=int, default=320)
    parser.add_argument("--seed", type=int, default=2026)
    parser.add_argument("--include-scans", action="store_true")
    parser.add_argument("--limit", type=int, default=None, help="first N invoices only")
    parser.add_argument("--out", type=Path, default=RESULTS)
    parser.add_argument("--report", type=Path, default=REPORT)
    args = parser.parse_args(argv)
    result = asyncio.run(_run(args.count, args.seed, args.include_scans, args.limit))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    write_report(args.report, result)
    print(
        json.dumps(
            {
                "duplicate_recall": result["duplicates"]["recall"],
                "duplicate_precision": result["duplicates"]["precision"],
                "bank_mismatch_recall": result["bank_mismatch"]["recall"],
                "lookalike_recall": result["lookalike_domain"]["recall"],
                "arithmetic_vat_recall": result["arithmetic_vat"]["recall"],
                "injection_catch_rate": result["injection"]["catch_rate"],
                "clean_review_rate": result["false_positives"]["review_rate"],
                "matching_exception_recall": result["matching"]["exception_recall"],
                "matching_exception_precision": result["matching"]["exception_precision"],
                "matching_match_rate": result["matching"]["match_rate"],
                "seconds": result["seconds"],
            },
            indent=1,
        )
    )
    print(f"wrote {args.out} and {args.report}")
    return 0
