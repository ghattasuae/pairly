# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""``uv run python -m synth.hire``: the synthetic Hire dataset.

    python -m synth.hire generate --out <dir> [--count N] [--seed S]
        Render every document into <dir> (cvs/, attacks/, fairness/) and write
        <dir>/index.json: every document with its file, ground truth and labels.
    python -m synth.hire manifests [--count N] [--seed S]
        Rewrite the committed ground-truth manifests (synth/hire/manifests/) and the
        scripted model responses (synth/hire/recordings/: the injection classifier, and
        the screening prompts for the demo applications).
    python -m synth.hire --check [--count N] [--seed S]
        Rebuild the manifests in memory and fail when a committed manifest differs
        from what the generators produce now. Only the recorded documents are rendered
        and parsed (for the recordings bundles).

The committed manifests are for the defaults (300 regular CVs, seed 2026).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from importlib.metadata import version
from pathlib import Path
from typing import Any, Final

from synth.hire import screening
from synth.hire.dataset import DEFAULT_COUNT, DEFAULT_SEED, DocSpec, build
from synth.hire.labels import rule_table
from synth.hire.recordings import BUNDLE_PATH, build_bundle

MANIFEST_DIR: Final = Path(__file__).resolve().parent / "manifests"
ROOT: Final = Path(__file__).resolve().parents[2]
GENERATOR_VERSION: Final = "1"


def _line(spec: DocSpec) -> str:
    return json.dumps(
        spec.record.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def stats(specs: list[DocSpec]) -> dict[str, Any]:
    records = [s.record for s in specs]

    def count(key: Any, items: Any = records) -> dict[str, int]:
        return dict(sorted(Counter(key(r) for r in items).items()))

    attacks = [r for r in records if r.attack is not None]
    techniques: Counter[str] = Counter()
    for r in attacks:
        if r.attack is not None:
            techniques.update(r.attack.techniques)
    return {
        "documents": len(records),
        "by_kind": count(lambda r: r.kind),
        "by_format": count(lambda r: r.format),
        "by_layout": count(lambda r: r.layout),
        "by_language": count(lambda r: r.language),
        "by_family": count(lambda r: r.family),
        "by_seniority": count(lambda r: r.seniority),
        "by_text_layer": count(lambda r: r.text_layer),
        "by_name_locale": count(lambda r: r.persona.name_locale),
        "attacks_by_caught_by": count(lambda r: r.attack.caught_by, attacks),  # type: ignore[union-attr]
        "attack_techniques": dict(sorted(techniques.items())),
        "fairness_pairs": sum(
            1 for r in records if r.fairness is not None and r.fairness.role == "variant"
        ),
    }


def manifests(count: int, seed: int) -> dict[Path, str]:
    specs = build(count, seed)
    files: dict[Path, str] = {}
    for kind, name in (
        ("cv", "cvs.jsonl"),
        ("attack", "attacks.jsonl"),
        ("fairness", "fairness.jsonl"),
    ):
        lines = [_line(s) for s in specs if s.record.kind == kind]
        files[MANIFEST_DIR / name] = "\n".join(lines) + "\n"
    pairs = []
    for s in specs:
        info = s.record.fairness
        if info is not None and info.role == "variant":
            pairs.append(
                {
                    "pair_id": info.pair_id,
                    "base": f"{info.pair_id}-base",
                    "variant": s.record.id,
                    "dimension": info.dimension,
                    "value": info.value,
                    "expected_label_change": 0,
                }
            )
    files[MANIFEST_DIR / "fairness_pairs.json"] = (
        json.dumps(pairs, ensure_ascii=False, indent=1) + "\n"
    )
    summary = {
        "generator_version": GENERATOR_VERSION,
        "count": count,
        "seed": seed,
        "faker": version("faker"),
        "labels": "synthetic, rule-based (synth/hire/labels.py); not human judgments",
        "label_rules": rule_table(),
        "stats": stats(specs),
    }
    files[MANIFEST_DIR / "summary.json"] = json.dumps(summary, ensure_ascii=False, indent=1) + "\n"
    if count == DEFAULT_COUNT and seed == DEFAULT_SEED:
        files[BUNDLE_PATH] = build_bundle(specs)
        files[screening.BUNDLE_PATH] = screening.build_bundle(specs)
    return files


def check(count: int, seed: int) -> int:
    stale = []
    for path, text in manifests(count, seed).items():
        current = path.read_text(encoding="utf-8") if path.exists() else None
        if current != text:
            stale.append(str(path.relative_to(ROOT)))
    if stale:
        print("out of date (run: uv run python -m synth.hire manifests):", file=sys.stderr)
        for name in stale:
            print(f"  {name}", file=sys.stderr)
        return 1
    print("synth.hire manifests match the generators")
    return 0


def write_manifests(count: int, seed: int) -> int:
    for path, text in manifests(count, seed).items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        print(f"wrote {path.relative_to(ROOT)}")
    return 0


def generate(out: Path, count: int, seed: int) -> int:
    started = time.monotonic()
    specs = build(count, seed)
    out.mkdir(parents=True, exist_ok=True)
    index = []
    total = 0
    for spec in specs:
        path = out / spec.record.file
        path.parent.mkdir(parents=True, exist_ok=True)
        data = spec.render()
        path.write_bytes(data)
        total += len(data)
        index.append(spec.record.model_dump(mode="json"))
    summary = stats(specs)
    (out / "index.json").write_text(
        json.dumps(
            {"count": count, "seed": seed, "stats": summary, "documents": index},
            ensure_ascii=False,
            indent=1,
        )
        + "\n",
        encoding="utf-8",
    )
    elapsed = time.monotonic() - started
    print(
        json.dumps(
            {
                "out": str(out),
                "files": len(specs),
                "bytes": total,
                "seconds": round(elapsed, 1),
                "stats": summary,
            },
            indent=1,
        )
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m synth.hire")
    parser.add_argument("--check", action="store_true", help="compare the committed manifests")
    parser.add_argument("--count", type=int, default=DEFAULT_COUNT)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    sub = parser.add_subparsers(dest="command")
    gen = sub.add_parser("generate", help="render the dataset into a directory")
    gen.add_argument("--out", type=Path, required=True)
    gen.add_argument("--count", type=int, default=None)
    gen.add_argument("--seed", type=int, default=None)
    man = sub.add_parser("manifests", help="rewrite the committed manifests")
    man.add_argument("--count", type=int, default=None)
    man.add_argument("--seed", type=int, default=None)
    args = parser.parse_args(argv)
    count = getattr(args, "count", None) or DEFAULT_COUNT
    seed = args.seed if getattr(args, "seed", None) is not None else DEFAULT_SEED
    if count < 1 or count > 5000:
        parser.error("--count must be 1..5000")
    if args.check:
        return check(count, seed)
    if args.command == "generate":
        return generate(args.out, count, seed)
    if args.command == "manifests":
        return write_manifests(count, seed)
    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
