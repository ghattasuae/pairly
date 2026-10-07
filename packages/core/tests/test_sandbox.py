# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""The parser sandbox: real parses succeed, misbehaving parsers are stopped (fail closed).

Limits are kept small so the suite stays quick and cool: about 5 seconds of CPU total.
"""

from __future__ import annotations

import json
import os
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from pairly_core.documents import sandbox_child
from pairly_core.documents.model import NormalizedDocument, OcrInfo, Page, ParserInfo
from pairly_core.documents.pipeline import ParseConfig, _apply_vision
from pairly_core.documents.registry import ParseOptions
from pairly_core.documents.sandbox import SandboxLimits, process_memory, run_sandboxed
from pairly_core.ingestion import sniff as sn
from pairly_core.ingestion.settings import load_ingestion_settings

from . import doc_fixtures as fx

PROBES = "pairly_core.documents.probes"


@pytest.fixture
def pdf(tmp_path: Path) -> Path:
    path = tmp_path / "input.pdf"
    path.write_bytes(fx.tricky_pdf())
    return path


async def test_real_parse_in_sandbox(pdf: Path, tmp_path: Path) -> None:
    result = await run_sandboxed(pdf, sn.PDF, workdir=tmp_path)
    assert result.ok, result.detail
    assert result.document is not None
    assert result.document.media_type == sn.PDF
    assert "Jordan Example" in result.document.text


async def test_child_gets_no_secrets_or_network(
    pdf: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PAIRLY_DATABASE_URL", "postgresql://user:pw@db/x")
    monkeypatch.setenv("PAIRLY_S3_SECRET_KEY", "synthetic-secret")
    echo = await run_sandboxed(pdf, sn.PDF, workdir=tmp_path, parser=f"{PROBES}:echo")
    assert echo.ok
    assert echo.document is not None
    assert echo.document.metadata["env_has_secrets"] == "false"
    blocked = await run_sandboxed(pdf, sn.PDF, workdir=tmp_path, parser=f"{PROBES}:network")
    assert (blocked.ok, blocked.error) == (False, "network_blocked")


async def test_cpu_limit(pdf: Path, tmp_path: Path) -> None:
    limits = SandboxLimits(wall_seconds=20, cpu_seconds=1)
    result = await run_sandboxed(
        pdf, sn.PDF, workdir=tmp_path, limits=limits, parser=f"{PROBES}:loop"
    )
    assert (result.ok, result.error) == (False, "cpu_limit")


async def test_wall_clock_limit(pdf: Path, tmp_path: Path) -> None:
    limits = SandboxLimits(wall_seconds=1.0, cpu_seconds=60)
    result = await run_sandboxed(
        pdf, sn.PDF, workdir=tmp_path, limits=limits, parser=f"{PROBES}:loop"
    )
    assert (result.ok, result.error) == (False, "timeout")
    assert result.elapsed < 5


async def test_memory_limit(pdf: Path, tmp_path: Path) -> None:
    limits = SandboxLimits(wall_seconds=20, memory_bytes=192 * 1024 * 1024)
    result = await run_sandboxed(
        pdf, sn.PDF, workdir=tmp_path, limits=limits, parser=f"{PROBES}:allocate"
    )
    assert (result.ok, result.error) == (False, "memory_limit")


@pytest.mark.parametrize(
    ("probe", "error"),
    [("crash", "crashed"), ("garbage", "invalid_output")],
)
async def test_crash_and_garbage(pdf: Path, tmp_path: Path, probe: str, error: str) -> None:
    result = await run_sandboxed(pdf, sn.PDF, workdir=tmp_path, parser=f"{PROBES}:{probe}")
    assert (result.ok, result.error) == (False, error)


async def test_parse_errors_and_output_limit(tmp_path: Path) -> None:
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"%PDF-1.4 nothing")
    result = await run_sandboxed(bad, sn.PDF, workdir=tmp_path)
    assert (result.ok, result.error) == (False, "corrupt")
    unknown = await run_sandboxed(bad, "application/zip", workdir=tmp_path)
    assert unknown.error == "unsupported_media_type"
    good = tmp_path / "good.pdf"
    good.write_bytes(fx.tricky_pdf())
    tiny = SandboxLimits(max_output_bytes=2000)
    limited = await run_sandboxed(good, sn.PDF, workdir=tmp_path, limits=tiny)
    assert (limited.ok, limited.error) == (False, "output_limit")


def test_process_memory_of_self() -> None:
    rss = process_memory(os.getpid())
    assert rss is None or rss > 1024 * 1024


def test_child_main_in_process(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Exercise the child entry point without applying limits to the test process."""
    monkeypatch.setattr(sandbox_child, "apply_limits", lambda **_: None)
    monkeypatch.setattr(sandbox_child, "block_network", lambda: None)
    source = tmp_path / "in.txt"
    source.write_bytes(b"Invoice 1\nTotal 2")
    output = tmp_path / "out.json"

    def run(*extra: str, media: str = sn.TEXT) -> int:
        args: Sequence[str] = [
            "--input",
            str(source),
            "--media-type",
            media,
            "--output",
            str(output),
            "--options",
            ParseOptions().model_dump_json(),
            "--cpu-seconds",
            "5",
            "--memory-bytes",
            str(512 * 1024 * 1024),
            "--max-output-bytes",
            "1000000",
            *extra,
        ]
        return sandbox_child.main(list(args))

    assert run() == 0
    assert NormalizedDocument.model_validate_json(output.read_bytes()).pages[0].text
    assert run(media="application/zip") == 2
    assert json.loads(output.read_bytes())["code"] == "unsupported_media_type"
    assert sandbox_child.main(["--input"]) == sandbox_child.EXIT_USAGE
    with pytest.raises(SystemExit):
        run("--parser", "os:system")


def test_child_limits_helpers() -> None:
    sandbox_child._limit("RLIMIT_NOT_A_REAL_LIMIT", 1)  # unknown names are ignored


# ------------------------------------------------------------------ vision fallback


def _ocr_doc(confidence: float | None) -> NormalizedDocument:
    return NormalizedDocument(
        media_type=sn.PNG,
        parser=ParserInfo(name="image-ocr", version="1"),
        pages=[
            Page(
                number=1,
                width=10,
                height=10,
                unit="px",
                text="blurry",
                ocr=OcrInfo(engine="t", languages="eng", mean_confidence=confidence, words=1),
            )
        ],
    )


class _Vision:
    def __init__(self, result: NormalizedDocument | None = None, fail: bool = False) -> None:
        self.result, self.fail = result, fail
        self.pages: list[int] = []

    async def transcribe(self, **kwargs: Any) -> NormalizedDocument | None:
        self.pages = list(kwargs["pages"])
        if self.fail:
            raise RuntimeError("model down")
        return self.result


async def test_low_confidence_without_fallback_needs_review(tmp_path: Path) -> None:
    doc = await _apply_vision(
        _ocr_doc(20), path=tmp_path, media_type=sn.PNG, threshold=60, vision=None
    )
    assert doc.warning_codes() == ["low_ocr_confidence"]
    unchanged = await _apply_vision(
        _ocr_doc(95), path=tmp_path, media_type=sn.PNG, threshold=60, vision=None
    )
    assert unchanged.warnings == []


async def test_vision_fallback_used_and_fails_closed(tmp_path: Path) -> None:
    better = _ocr_doc(None).model_copy(update={"parser": ParserInfo(name="vision", version="1")})
    vision = _Vision(result=better)
    doc = await _apply_vision(
        _ocr_doc(None), path=tmp_path, media_type=sn.PNG, threshold=60, vision=vision
    )
    assert doc.parser.name == "vision"
    assert vision.pages == [1]
    for broken in (_Vision(fail=True), _Vision(result=None)):
        doc = await _apply_vision(
            _ocr_doc(10), path=tmp_path, media_type=sn.PNG, threshold=60, vision=broken
        )
        assert doc.warning_codes() == ["low_ocr_confidence"]


def test_parse_config_from_settings() -> None:
    settings = load_ingestion_settings(
        env="test",
        s3_bucket="b",
        s3_access_key="a",
        s3_secret_key="s",  # noqa: S106 (test value)
        parse_memory_mb=256,
        ocr_languages="eng",
    )
    config = ParseConfig.from_settings(settings)
    assert config.limits.memory_bytes == 256 * 1024 * 1024
    assert config.options.ocr_languages == "eng"
