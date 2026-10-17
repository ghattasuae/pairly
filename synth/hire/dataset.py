# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Build the synthetic Hire dataset: regular CVs, attack CVs and fairness pairs.

``build(count, seed)`` returns every document as a ``DocSpec``: its manifest record
(ground truth, labels, attack or fairness info) and a ``render()`` that produces the
file bytes on demand. Manifests can therefore be rebuilt and checked without
rendering a single file (``python -m synth.hire --check``).
"""

from __future__ import annotations

import io
import random
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Final

from PIL import Image, ImageDraw

from pairly_core.decisions.bidi import has_rtl
from synth.hire.facts import CvFacts, make_facts, make_person, stable_seed
from synth.hire.jobs import load_jobs
from synth.hire.labels import label_all
from synth.hire.models import (
    AttackInfo,
    DocumentRecord,
    FairnessInfo,
    Format,
    Layout,
    Persona,
)
from synth.hire.render_docx import Run, render_docx
from synth.hire.render_image import render_scan, scan_format
from synth.hire.render_pdf import PdfStyle, render_pdf
from synth.hire.text import CvText, DateStyle, Language, build_text, ground_truth

DEFAULT_COUNT: Final = 300
DEFAULT_SEED: Final = 2026
ACCENTS: Final = ((0.1, 0.2, 0.45), (0.0, 0.38, 0.4), (0.45, 0.1, 0.15), (0.15, 0.15, 0.15))
EXTENSIONS: Final = {"pdf": "pdf", "docx": "docx", "png": "png", "jpg": "jpg", "scan_pdf": "pdf"}


@dataclass
class RenderPlan:
    """How to draw a document, plus optional attack hooks."""

    format: Format
    layout: Layout
    date_style: DateStyle
    pdf_style: PdfStyle = field(default_factory=PdfStyle)
    jpeg: bool = False
    photo: bool = False
    pdf_extra: Callable[[Any], None] | None = None
    pdf_metadata: dict[str, str] = field(default_factory=dict)
    xmp: bytes | None = None
    docx_extra: list[list[Run] | str] = field(default_factory=list)
    docx_props: dict[str, str] = field(default_factory=dict)
    docx_custom: dict[str, str] = field(default_factory=dict)


@dataclass
class DocSpec:
    record: DocumentRecord
    facts: CvFacts
    text: CvText
    plan: RenderPlan
    seed: int

    def render(self) -> bytes:
        plan = self.plan
        author = self.record.ground_truth.contact.name
        if plan.format == "pdf":
            data = render_pdf(
                self.text,
                plan.layout,
                style=plan.pdf_style,
                photo=plan.photo,
                extra=plan.pdf_extra,
                metadata=plan.pdf_metadata,
                author=author,
            )
            if plan.xmp is not None:
                data = with_xmp(data, plan.xmp)
            return data
        if plan.format == "docx":
            return render_docx(
                self.text,
                plan.layout,
                photo_png=photo_png() if plan.photo else None,
                extra=plan.docx_extra,
                props=plan.docx_props,
                custom=plan.docx_custom,
                author=author,
            )
        rng = random.Random(stable_seed(self.seed, self.record.id, "scan"))
        result = render_scan(self.text, plan.layout, rng, jpeg=plan.jpeg)
        if result.format != plan.format:
            raise RuntimeError(f"{self.record.id}: scan format drifted")
        return result.data


def with_xmp(data: bytes, xmp: bytes) -> bytes:
    from pypdf import PdfReader, PdfWriter
    from pypdf.generic import NameObject, StreamObject

    writer = PdfWriter(clone_from=PdfReader(io.BytesIO(data)))
    stream = StreamObject()
    stream.set_data(xmp)
    stream.update(
        {NameObject("/Type"): NameObject("/Metadata"), NameObject("/Subtype"): NameObject("/XML")}
    )
    writer._root_object[NameObject("/Metadata")] = writer._add_object(stream)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


_PHOTO: bytes | None = None


def photo_png() -> bytes:
    """A grey silhouette placeholder (no face, no person)."""
    global _PHOTO
    if _PHOTO is None:
        image = Image.new("RGB", (120, 140), (210, 214, 222))
        draw = ImageDraw.Draw(image)
        draw.ellipse((38, 22, 82, 66), fill=(150, 158, 172))
        draw.ellipse((18, 74, 102, 150), fill=(150, 158, 172))
        buf = io.BytesIO()
        image.save(buf, format="PNG", optimize=False)
        _PHOTO = buf.getvalue()
    return _PHOTO


def _weighted(rng: random.Random, weights: dict[str, int]) -> str:
    keys = sorted(weights)
    return rng.choices(keys, weights=[weights[k] for k in keys], k=1)[0]


def text_layer(fmt: str, text: CvText) -> str:
    if fmt in ("png", "jpg", "scan_pdf"):
        return "image"
    if fmt == "pdf" and any(has_rtl(line.text) for line in text.lines):
        return "visual_shaped"
    return "logical"


def make_spec(
    doc_id: str,
    kind: str,
    facts: CvFacts,
    language: Language,
    plan: RenderPlan,
    seed: int,
    *,
    attack: AttackInfo | None = None,
    fairness: FairnessInfo | None = None,
    text: CvText | None = None,
) -> DocSpec:
    jobs = list(load_jobs())
    text = text or build_text(facts, language, plan.date_style)
    if plan.format in ("png", "jpg", "scan_pdf"):
        plan.format = scan_format(text, plan.layout, jpeg=plan.jpeg)  # type: ignore[assignment]
        plan.photo = False
    person = facts.person
    persona = Persona(
        gender=person.gender,  # type: ignore[arg-type]
        nationality=person.nationality,
        birth_year=person.birth_year,
        photo=plan.photo,
        name_locale=person.locale,
        personal_details_shown=facts.personal_details_shown,
    )
    folder = {"cv": "cvs", "attack": "attacks", "fairness": "fairness"}[kind]
    filename = attack.filename if attack is not None and attack.filename else None
    ext = EXTENSIONS[plan.format]
    record = DocumentRecord(
        id=doc_id,
        kind=kind,  # type: ignore[arg-type]
        file=f"{folder}/{filename or f'{doc_id}.{ext}'}",
        format=plan.format,
        layout=plan.layout,
        language=language,
        family=facts.family,
        seniority=facts.level,  # type: ignore[arg-type]
        text_layer=text_layer(plan.format, text),  # type: ignore[arg-type]
        ground_truth=ground_truth(facts, text, plan.date_style),
        persona=persona,
        labels=label_all(jobs, facts, text),
        attack=attack,
        fairness=fairness,
    )
    return DocSpec(record=record, facts=facts, text=text, plan=plan, seed=seed)


def regular_cvs(count: int, seed: int) -> list[DocSpec]:
    specs: list[DocSpec] = []
    for i in range(count):
        doc_id = f"cv-{i + 1:04d}"
        rng = random.Random(stable_seed(seed, doc_id, "layout"))
        language: Language = _weighted(rng, {"en": 76, "ar": 12, "bilingual": 12})  # type: ignore[assignment]
        person = None
        if language != "en":
            person = make_person(
                random.Random(stable_seed(seed, doc_id, "person")),
                stable_seed(seed, doc_id, "name"),
                locale="arab",
            )
        facts = make_facts(doc_id, seed, person=person)
        if language == "en":
            fmt = _weighted(rng, {"pdf": 45, "docx": 35, "image": 20})
        elif language == "ar":
            fmt = _weighted(rng, {"pdf": 25, "docx": 50, "image": 25})
        else:
            fmt = _weighted(rng, {"pdf": 50, "docx": 50})
        if language == "ar":
            layout = "arabic"
        elif language == "bilingual":
            layout = "bilingual"
        elif fmt == "pdf":
            layout = _weighted(
                rng, {"single_column": 35, "two_column": 25, "sidebar": 20, "table": 20}
            )
        elif fmt == "docx":
            layout = _weighted(
                rng, {"single_column": 45, "table": 30, "two_column": 15, "sidebar": 10}
            )
        else:
            layout = _weighted(rng, {"single_column": 60, "two_column": 40})
        styles: tuple[DateStyle, ...] = (
            ("mon_year", "numeric") if language == "ar" else ("mon_year", "numeric", "iso", "year")
        )
        date_style = rng.choice(styles)
        plan = RenderPlan(
            format="png" if fmt == "image" else fmt,  # type: ignore[arg-type]
            layout=layout,  # type: ignore[arg-type]
            date_style=date_style,
            pdf_style=PdfStyle(
                accent=rng.choice(ACCENTS),
                grey_dates=rng.random() < 0.5,
                footer=rng.random() < 0.6,
                body_size=rng.choice((9.5, 10.0, 10.5)),
            ),
            jpeg=fmt == "image" and rng.random() < 0.5,
            photo=facts.person.photo,
        )
        if layout == "arabic" and plan.format == "png" and not plan.jpeg:
            plan.jpeg = rng.random() < 0.5
        if plan.format == "png" and plan.jpeg:
            plan.format = "jpg"
        specs.append(make_spec(doc_id, "cv", facts, language, plan, seed))
    return specs


def build(count: int = DEFAULT_COUNT, seed: int = DEFAULT_SEED) -> list[DocSpec]:
    from synth.hire.attacks import attack_cvs
    from synth.hire.fairness import fairness_cvs

    return [*regular_cvs(count, seed), *attack_cvs(seed), *fairness_cvs(seed)]
