# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Media type sniffing from file bytes.

The client's ``Content-Type`` and file extension are never trusted. The type comes from
signatures (magic numbers) and, for ZIP based Office files, from the package's
``[Content_Types].xml``. Anything not recognised is ``application/octet-stream``, which
no intake policy may allow.

DOCX is recognised only as a plain Word document: macro-enabled documents (``.docm``),
templates and other Office formats sniff as ``application/zip`` and are refused by the
default policies. So does a package that says it is a plain ``.docx`` but carries active
content (``active_content``): a VBA project or ActiveX parts (security review 2026-10,
L-04), or a relationship to anything outside the package, such as a remote attached
template (remote template injection) or an external image, frame or OLE object (L-12).
Hyperlinks are the one external relationship kept: Word never follows one by itself.
Intake names the refusal (``RejectReason.ACTIVE_CONTENT``).
"""

from __future__ import annotations

import zipfile
from pathlib import Path
from typing import Final

from defusedxml import DefusedXmlException
from defusedxml import ElementTree as SafeET

PDF: Final = "application/pdf"
PNG: Final = "image/png"
JPEG: Final = "image/jpeg"
TIFF: Final = "image/tiff"
DOCX: Final = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
XML: Final = "application/xml"
ZIP: Final = "application/zip"
OLE: Final = "application/x-ole-storage"
TEXT: Final = "text/plain"
OCTET: Final = "application/octet-stream"

# Every type the platform can name in an intake policy. ``text/plain`` is never sniffed
# from uploads (it cannot be told apart from arbitrary bytes); it is only used for email
# bodies that Pairly itself extracts.
KNOWN_TYPES: Final = frozenset({PDF, PNG, JPEG, TIFF, DOCX, XML, TEXT})
# text/plain is safe to allow: uploads never sniff as text, only email bodies use it.
DEFAULT_ALLOWED_TYPES: Final = frozenset({PDF, PNG, JPEG, TIFF, DOCX, TEXT})

# Bytes the caller must read before calling ``sniff``.
SNIFF_BYTES: Final = 2048

_DOCX_MAIN = "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"
_CT_NS = "{http://schemas.openxmlformats.org/package/2006/content-types}"
# Ceiling on the [Content_Types].xml we read from an untrusted ZIP.
_MAX_CONTENT_TYPES_BYTES = 256 * 1024
_MAX_ZIP_ENTRIES = 10_000
# Ceiling on one relationships part (``*.rels``) and on how many are read.
_MAX_RELS_BYTES = 256 * 1024
_MAX_RELS_PARTS = 200
# The only relationship allowed to point outside the package: a hyperlink, which Word
# opens only when a person clicks it. Every other external target (an attached
# template, an image, a frame, an OLE object, a subdocument) is fetched on opening.
_HYPERLINK_TYPES: Final = frozenset(
    {
        "http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink",
        "http://purl.oclc.org/ooxml/officeDocument/relationships/hyperlink",
    }
)
_TEMPLATE_SUFFIX = "/attachedtemplate"

# Why a would-be DOCX is refused (``active_content``).
MACROS: Final = "macros"
EXTERNAL_TEMPLATE: Final = "external_template"
EXTERNAL_RELATIONSHIP: Final = "external_relationship"
UNREADABLE_RELATIONSHIPS: Final = "unreadable_relationships"


def sniff(head: bytes, path: Path | None = None) -> str:
    """The media type of a file, from its first bytes (``head``) and, for ZIPs, ``path``."""
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return PNG
    if head.startswith(b"\xff\xd8\xff"):
        return JPEG
    if head.startswith((b"II*\x00", b"MM\x00*")):
        return TIFF
    # PDF readers accept the header anywhere in the first 1024 bytes.
    if b"%PDF-" in head[:1024]:
        return PDF
    if head.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        return OLE
    if head.startswith(b"PK\x03\x04"):
        return _sniff_zip(path) if path is not None else ZIP
    if _looks_like_xml(head):
        return XML
    return OCTET


def _looks_like_xml(head: bytes) -> bool:
    text = head
    for bom in (b"\xef\xbb\xbf",):
        if text.startswith(bom):
            text = text[len(bom) :]
    return text.lstrip(b" \t\r\n").startswith(b"<?xml")


def _sniff_zip(path: Path) -> str:
    """DOCX if the package declares a Word main document part, else plain ZIP."""
    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            if len(infos) > _MAX_ZIP_ENTRIES:
                return ZIP
            names = {info.filename for info in infos}
            if "[Content_Types].xml" not in names or "word/document.xml" not in names:
                return ZIP
            if _active_content_of(archive, infos) is not None:
                # Macro or ActiveX parts, or a relationship outside the package, in a
                # package that says it is a plain .docx: never a DOCX, so every intake
                # policy refuses it.
                return ZIP
            info = archive.getinfo("[Content_Types].xml")
            if info.file_size > _MAX_CONTENT_TYPES_BYTES:
                return ZIP
            with archive.open(info) as handle:
                data = handle.read(_MAX_CONTENT_TYPES_BYTES + 1)
    except (zipfile.BadZipFile, OSError, KeyError, RuntimeError, NotImplementedError):
        return ZIP
    if len(data) > _MAX_CONTENT_TYPES_BYTES:
        return ZIP
    try:
        root = SafeET.fromstring(data, forbid_dtd=True)
    except (SafeET.ParseError, DefusedXmlException):
        return ZIP
    for override in root.iter(f"{_CT_NS}Override"):
        if (
            override.get("PartName") == "/word/document.xml"
            and override.get("ContentType") == _DOCX_MAIN
        ):
            return DOCX
    return ZIP


def active_content(path: Path) -> str | None:
    """Why a ZIP that looks like a Word document is refused as one: ``MACROS`` (a VBA
    project or ActiveX parts), ``EXTERNAL_TEMPLATE`` (a remote attached template),
    ``EXTERNAL_RELATIONSHIP`` (any other target outside the package but a hyperlink) or
    ``UNREADABLE_RELATIONSHIPS`` (a relationships part too large or not parseable: fail
    closed). ``None`` for anything else, including a ZIP that is no Word document."""
    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            if len(infos) > _MAX_ZIP_ENTRIES:
                return None
            names = {info.filename for info in infos}
            if "[Content_Types].xml" not in names or "word/document.xml" not in names:
                return None
            return _active_content_of(archive, infos)
    except (zipfile.BadZipFile, OSError, KeyError, RuntimeError, NotImplementedError):
        return None


def _active_content_of(archive: zipfile.ZipFile, infos: list[zipfile.ZipInfo]) -> str | None:
    if any(_active_content(info.filename) for info in infos):
        return MACROS
    rels = [info for info in infos if info.filename.lower().endswith(".rels")]
    if len(rels) > _MAX_RELS_PARTS:
        return UNREADABLE_RELATIONSHIPS
    found: str | None = None
    for info in rels:
        kind = _external_kind(archive, info)
        if kind in (EXTERNAL_TEMPLATE, UNREADABLE_RELATIONSHIPS):
            return kind
        found = found or kind
    return found


def _external_kind(archive: zipfile.ZipFile, info: zipfile.ZipInfo) -> str | None:
    """What one relationships part points at outside the package, if anything."""
    if info.file_size > _MAX_RELS_BYTES:
        return UNREADABLE_RELATIONSHIPS
    with archive.open(info) as handle:
        data = handle.read(_MAX_RELS_BYTES + 1)
    if len(data) > _MAX_RELS_BYTES:
        return UNREADABLE_RELATIONSHIPS
    try:
        root = SafeET.fromstring(data, forbid_dtd=True)
    except (SafeET.ParseError, DefusedXmlException):
        return UNREADABLE_RELATIONSHIPS
    found: str | None = None
    for rel in root.iter():
        if not isinstance(rel.tag, str) or rel.tag.rsplit("}", 1)[-1] != "Relationship":
            continue
        if (rel.get("TargetMode") or "").strip().lower() != "external":
            continue
        kind = (rel.get("Type") or "").strip()
        if kind.lower().endswith(_TEMPLATE_SUFFIX):
            return EXTERNAL_TEMPLATE
        if kind not in _HYPERLINK_TYPES:
            found = EXTERNAL_RELATIONSHIP
    return found


def _active_content(name: str) -> bool:
    """A package part that carries code: a VBA project (``vbaProject.bin``, its
    ``vbaData.xml``) or an ActiveX control (``word/activeX/``)."""
    lower = name.lower()
    base = lower.rsplit("/", 1)[-1]
    return base in {"vbaproject.bin", "vbadata.xml"} or lower.startswith("word/activex/")


def extension_for(media_type: str) -> str:
    """A file extension for a sniffed type (used for temp files and download names)."""
    return {
        PDF: ".pdf",
        PNG: ".png",
        JPEG: ".jpg",
        TIFF: ".tif",
        DOCX: ".docx",
        XML: ".xml",
        TEXT: ".txt",
    }.get(media_type, ".bin")
