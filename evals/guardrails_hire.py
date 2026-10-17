# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Deterministic guardrails on the synthetic Hire corpus (WP2.4b).

    uv run python -m evals.guardrails_hire [--count N] [--seed S] [--include-scans]

Builds the ``synth.hire`` dataset in memory, parses every PDF and DOCX with the same
parsers the sandbox runs (in process, renderer check included), scans each with
``scan_document`` and writes ``evals/results/guardrails_hire.json``:

* document level, over guardrail-scoped attack CVs (``expected_findings`` not empty)
  against clean CVs (regular and fairness CVs): precision, recall, false-positive
  rate. An attack is detected when the report is flagged (a medium or high finding);
  a clean CV is a false positive when it is flagged.
* recall per ``caught_by`` group and per technique, type-level recall (every expected
  finding type flagged), payload leaks after ``sanitize_for_model``, detector errors.
* claim-consistency attacks (dates, titles, unsupported skills) are not guardrail
  findings: reported apart, never counted as misses or false positives.
* every false positive with its finding codes, so threshold decisions can be made.
* label evidence validity: the share of rubric label evidence quotes found in the
  parsed text, per format and layout (clean CVs in every
  language; scans excluded).

The optional model classifier is not run here (it needs a live model; the committed
recordings are scripted test answers, not model output). Thresholds are not changed.
Scans (PNG, JPEG, image PDF) need OCR and are skipped unless ``--include-scans``.
"""

from __future__ import annotations

import argparse
import json
import statistics
import tempfile
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Final

from pairly_core.documents.docx import DocxParser
from pairly_core.documents.model import NormalizedDocument
from pairly_core.documents.ocr import ImageParser
from pairly_core.documents.pdf import PdfParser
from pairly_core.documents.registry import ParseOptions
from pairly_core.guardrails import ScanContext, quote_exists, sanitize_for_model, scan_document
from pairly_core.guardrails.base import GuardrailConfig
from synth.hire.dataset import DEFAULT_COUNT, DEFAULT_SEED, DocSpec, build

RESULTS: Final = Path(__file__).resolve().parent / "results" / "guardrails_hire.json"
OPTIONS: Final = ParseOptions(ocr_languages="eng+ara")
SCANS: Final = frozenset({"png", "jpg", "scan_pdf"})


def parse(spec: DocSpec, workdir: Path) -> NormalizedDocument:
    record = spec.record
    path = workdir / Path(record.file).name
    path.write_bytes(spec.render())
    if record.format == "docx":
        return DocxParser().parse(path, OPTIONS)
    if record.format in ("png", "jpg"):
        return ImageParser().parse(path, OPTIONS)
    return PdfParser().parse(path, OPTIONS)


def _ratio(num: int, den: int) -> float | None:
    return round(num / den, 4) if den else None


def run(count: int, seed: int, include_scans: bool) -> dict[str, Any]:
    specs = build(count, seed)
    config = GuardrailConfig()
    rows: list[dict[str, Any]] = []
    parse_seconds: list[float] = []
    evidence_found: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    with tempfile.TemporaryDirectory(prefix="pairly-eval-hire-") as tmp:
        for spec in specs:
            record = spec.record
            if record.format in SCANS and not include_scans:
                continue
            started = time.monotonic()
            doc = parse(spec, Path(tmp))
            if record.format == "pdf":
                parse_seconds.append(time.monotonic() - started)
            attack = record.attack
            filename = attack.filename if attack and attack.filename else Path(record.file).name
            report = scan_document(
                doc, context=ScanContext(filename=filename, doc_kind="cv"), config=config
            )
            flagged_types = sorted({f.type.value for f in report.flagged_findings()})
            sanitized = sanitize_for_model(doc, report).text
            row: dict[str, Any] = {
                "id": record.id,
                "kind": record.kind,
                "format": record.format,
                "layout": record.layout,
                "language": record.language,
                "flagged": report.flagged,
                "errors": list(report.errors),
                "flagged_types": flagged_types,
                "codes": sorted({f"{f.type.value}.{f.code}" for f in report.flagged_findings()}),
            }
            if attack is not None:
                row.update(
                    attack_id=attack.attack_id,
                    techniques=list(attack.techniques),
                    caught_by=attack.caught_by,
                    expected=list(attack.expected_findings),
                    missing=sorted(set(attack.expected_findings) - set(flagged_types)),
                    leaked=[p for p in attack.payloads if p and p in sanitized],
                )
            rows.append(row)
            if record.kind != "attack" and record.text_layer != "image":
                text = doc.text
                for labels in record.labels:
                    for crit in labels.criteria:
                        for quote in crit.evidence:
                            hit = evidence_found[f"{record.format}/{record.layout}"]
                            hit[1] += 1
                            hit[0] += int(quote_exists(text, quote))
    return summarize(rows, parse_seconds, evidence_found, count, seed, include_scans)


def summarize(
    rows: list[dict[str, Any]],
    parse_seconds: list[float],
    evidence_found: dict[str, list[int]],
    count: int,
    seed: int,
    include_scans: bool,
) -> dict[str, Any]:
    clean = [r for r in rows if r["kind"] != "attack"]
    attacks = [r for r in rows if r["kind"] == "attack" and r["expected"]]
    claims = [r for r in rows if r["kind"] == "attack" and not r["expected"]]
    tp = sum(1 for r in attacks if r["flagged"])
    fn = len(attacks) - tp
    fp = sum(1 for r in clean if r["flagged"])
    tn = len(clean) - fp
    deterministic = [r for r in attacks if r["caught_by"] != "classifier"]
    det_tp = sum(1 for r in deterministic if r["flagged"])
    by_group: dict[str, dict[str, Any]] = {}
    for group in sorted({r["caught_by"] for r in attacks}):
        items = [r for r in attacks if r["caught_by"] == group]
        hit = sum(1 for r in items if r["flagged"])
        by_group[group] = {
            "attacks": len(items),
            "detected": hit,
            "recall": _ratio(hit, len(items)),
        }
    techniques: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for r in attacks:
        for t in r["techniques"]:
            techniques[t][1] += 1
            techniques[t][0] += int(r["flagged"])
    fp_by_layout: Counter[str] = Counter(
        f"{r['format']}/{r['layout']}" for r in clean if r["flagged"]
    )
    clean_by_layout: Counter[str] = Counter(f"{r['format']}/{r['layout']}" for r in clean)
    return {
        "dataset": {
            "generator": "synth.hire",
            "count": count,
            "seed": seed,
            "scans_included": include_scans,
        },
        "thresholds": "GuardrailConfig defaults, unchanged",
        "classifier": "not run: needs a live model (recordings are scripted test answers)",
        "documents": {
            "clean": len(clean),
            "guardrail_attacks": len(attacks),
            "claim_attacks": len(claims),
        },
        "document_level": {
            "true_positives": tp,
            "false_negatives": fn,
            "false_positives": fp,
            "true_negatives": tn,
            "precision": _ratio(tp, tp + fp),
            "recall": _ratio(tp, tp + fn),
            "false_positive_rate": _ratio(fp, fp + tn),
            "recall_without_classifier_attacks": _ratio(det_tp, len(deterministic)),
        },
        "type_level_recall": _ratio(
            sum(1 for r in attacks if r["flagged"] and not r["missing"]), len(attacks)
        ),
        "recall_by_caught_by": by_group,
        "recall_by_technique": {
            t: {"attacks": n, "detected": hit, "recall": _ratio(hit, n)}
            for t, (hit, n) in sorted(techniques.items())
        },
        "missed_attacks": [
            {
                k: r[k]
                for k in ("id", "caught_by", "techniques", "expected", "flagged_types", "errors")
            }
            for r in attacks
            if not r["flagged"]
        ],
        "incomplete_type_detection": [
            {k: r[k] for k in ("id", "expected", "missing", "flagged_types")}
            for r in attacks
            if r["flagged"] and r["missing"]
        ],
        "payload_leaks": sum(len(r["leaked"]) for r in attacks),
        "payload_leaks_by_document": {r["id"]: r["leaked"] for r in attacks if r["leaked"]},
        "detector_errors": sum(1 for r in rows if r["errors"]),
        "false_positives": [
            {k: r[k] for k in ("id", "format", "layout", "language", "codes")}
            for r in clean
            if r["flagged"]
        ],
        "false_positive_rate_by_layout": {
            key: {
                "clean": n,
                "flagged": fp_by_layout.get(key, 0),
                "rate": _ratio(fp_by_layout.get(key, 0), n),
            }
            for key, n in sorted(clean_by_layout.items())
        },
        "claim_attacks_flagged_by_guardrails": [r["id"] for r in claims if r["flagged"]],
        "label_evidence_found_rate_by_layout": {
            layout: {"quotes": n, "found": hit, "rate": _ratio(hit, n)}
            for layout, (hit, n) in sorted(evidence_found.items())
        },
        "pdf_parse_seconds": {
            "documents": len(parse_seconds),
            "mean": round(statistics.mean(parse_seconds), 3) if parse_seconds else None,
            "max": round(max(parse_seconds), 3) if parse_seconds else None,
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m evals.guardrails_hire")
    parser.add_argument("--count", type=int, default=DEFAULT_COUNT)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--include-scans", action="store_true")
    parser.add_argument("--out", type=Path, default=RESULTS)
    args = parser.parse_args(argv)
    result = run(args.count, args.seed, args.include_scans)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                k: result[k]
                for k in (
                    "documents",
                    "document_level",
                    "type_level_recall",
                    "payload_leaks",
                    "detector_errors",
                )
            },
            indent=1,
        )
    )
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
