# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""OCR with Tesseract (pytesseract), for images and scanned PDF pages.

Languages default to ``eng+ara``. Languages not installed are dropped with an
``ocr_language_missing`` warning (the document then goes to review); without the
``tesseract`` binary every OCR page gets ``ocr_unavailable``. Word boxes and per-word
confidences (0 to 100) are kept; lines become ``TextBlock``s.

The vision-model fallback for low confidence runs outside the sandbox (see
``pairly_core.documents.pipeline``); this module only measures confidence.
"""

from __future__ import annotations

import shutil
import warnings
from collections.abc import Iterator
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

from PIL import Image, ImageSequence

from pairly_core.documents.model import (
    BBox,
    NormalizedDocument,
    OcrInfo,
    Page,
    ParserInfo,
    ParseWarning,
    TextBlock,
    Word,
)
from pairly_core.documents.registry import ParseError, ParseOptions
from pairly_core.ingestion.sniff import JPEG, PNG, TIFF

ENGINE = "tesseract"


def _pytesseract() -> Any:
    import pytesseract  # type: ignore[import-untyped]

    return pytesseract


def tesseract_available() -> bool:
    cmd = _pytesseract().pytesseract.tesseract_cmd
    return shutil.which(cmd) is not None


@lru_cache(maxsize=1)
def tesseract_version() -> str:
    try:
        return str(_pytesseract().get_tesseract_version())
    except Exception:
        return "unknown"


@lru_cache(maxsize=1)
def installed_languages() -> frozenset[str]:
    try:
        return frozenset(_pytesseract().get_languages(config=""))
    except Exception:
        return frozenset()


def resolve_languages(requested: str) -> tuple[str, list[str]]:
    """``eng+ara`` -> (languages to use, missing ones). Falls back to ``eng``."""
    wanted = [lang for lang in requested.split("+") if lang]
    have = installed_languages()
    use = [lang for lang in wanted if lang in have]
    missing = [lang for lang in wanted if lang not in have]
    if not use:
        use = ["eng"] if "eng" in have else []
    return "+".join(use), missing


@dataclass
class OcrResult:
    words: list[Word] = field(default_factory=list)
    blocks: list[TextBlock] = field(default_factory=list)
    text: str = ""
    mean_confidence: float | None = None
    languages: str = ""


def ocr_image(image: Image.Image, *, languages: str, scale: float = 1.0) -> OcrResult:
    """OCR one image. ``scale`` converts pixel coordinates (for example to PDF points)."""
    pytesseract = _pytesseract()
    if image.mode not in {"RGB", "L"}:
        image = image.convert("RGB")
    data = pytesseract.image_to_data(
        image, lang=languages, config="--psm 3", output_type=pytesseract.Output.DICT
    )
    result = OcrResult(languages=languages)
    lines: dict[tuple[int, int, int], list[Word]] = {}
    confidences: list[float] = []
    for i, raw_text in enumerate(data.get("text", [])):
        text = str(raw_text).strip()
        try:
            conf = float(data["conf"][i])
        except (TypeError, ValueError):
            conf = -1.0
        if not text or conf < 0:
            continue
        left, top = float(data["left"][i]) * scale, float(data["top"][i]) * scale
        width, height = float(data["width"][i]) * scale, float(data["height"][i]) * scale
        word = Word(
            text=text,
            bbox=BBox(x0=left, top=top, x1=left + width, bottom=top + height),
            confidence=conf,
        )
        result.words.append(word)
        confidences.append(conf)
        key = (int(data["block_num"][i]), int(data["par_num"][i]), int(data["line_num"][i]))
        lines.setdefault(key, []).append(word)
    text_lines: list[str] = []
    for index, (_, words) in enumerate(sorted(lines.items())):
        line_text = " ".join(w.text for w in words)
        boxes = [w.bbox for w in words if w.bbox is not None]
        bbox = BBox(
            x0=min(b.x0 for b in boxes),
            top=min(b.top for b in boxes),
            x1=max(b.x1 for b in boxes),
            bottom=max(b.bottom for b in boxes),
        )
        conf = sum(w.confidence or 0.0 for w in words) / len(words)
        result.blocks.append(
            TextBlock(
                text=line_text, bbox=bbox, source="ocr", confidence=round(conf, 2), group=index
            )
        )
        text_lines.append(line_text)
    result.text = "\n".join(text_lines)
    if confidences:
        result.mean_confidence = round(sum(confidences) / len(confidences), 2)
    return result


def guarded_open(path: Path, max_pixels: int) -> Image.Image:
    """Open an image, refusing decompression bombs (more than ``max_pixels``)."""
    Image.MAX_IMAGE_PIXELS = max_pixels
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        try:
            image = Image.open(path)
            image.load()
        except (Image.DecompressionBombError, Image.DecompressionBombWarning):
            raise ParseError("image_too_large", "image has too many pixels") from None
        except (OSError, SyntaxError, ValueError) as exc:
            raise ParseError("corrupt", f"cannot decode image: {type(exc).__name__}") from None
    return image


def _frames(image: Image.Image) -> Iterator[Image.Image]:
    yield from ImageSequence.Iterator(image)


class ImageParser:
    name = "image-ocr"
    version = "1"
    media_types = frozenset({PNG, JPEG, TIFF})

    def parse(self, path: Path, options: ParseOptions) -> NormalizedDocument:
        image = guarded_open(path, options.max_image_pixels)
        doc_warnings: list[ParseWarning] = []
        available = tesseract_available()
        languages, missing = resolve_languages(options.ocr_languages) if available else ("", [])
        if not available:
            doc_warnings.append(
                ParseWarning(code="ocr_unavailable", message="tesseract is not installed")
            )
        elif missing:
            doc_warnings.append(
                ParseWarning(
                    code="ocr_language_missing",
                    message=f"OCR languages not installed: {'+'.join(missing)}",
                )
            )
        pages: list[Page] = []
        for index, frame in enumerate(_frames(image)):
            if index >= options.max_pages:
                doc_warnings.append(
                    ParseWarning(
                        code="pages_truncated", message=f"only {options.max_pages} pages read"
                    )
                )
                break
            frame_copy = frame.copy()
            if frame_copy.width * frame_copy.height > options.max_image_pixels:
                raise ParseError("image_too_large", "frame has too many pixels")
            page = Page(
                number=index + 1,
                width=float(frame_copy.width),
                height=float(frame_copy.height),
                unit="px",
            )
            if available and languages:
                result = ocr_image(frame_copy, languages=languages)
                page = page.model_copy(
                    update={
                        "text": result.text,
                        "blocks": result.blocks,
                        "words": result.words,
                        "ocr": OcrInfo(
                            engine=f"{ENGINE} {tesseract_version()}",
                            languages=languages,
                            mean_confidence=result.mean_confidence,
                            words=len(result.words),
                        ),
                    }
                )
            pages.append(page)
        if pages and not any(p.text.strip() for p in pages):
            doc_warnings.append(ParseWarning(code="no_text", message="no text found"))
        metadata = {
            f"image:{k}": str(v)[:512]
            for k, v in (image.info or {}).items()
            if isinstance(v, str | int | float)
        }
        metadata["image:format"] = str(image.format)
        return NormalizedDocument(
            media_type=Image.MIME.get(str(image.format), "image/unknown"),
            parser=ParserInfo(
                name=self.name,
                version=self.version,
                libraries={"tesseract": tesseract_version() if available else "missing"},
            ),
            pages=pages,
            metadata=metadata,
            warnings=doc_warnings,
        )
