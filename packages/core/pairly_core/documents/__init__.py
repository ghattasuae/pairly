# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Document understanding: turn a scanned-clean file into a ``NormalizedDocument``.

* ``model``      the normalised document model (pages, blocks, words, chars, tables).
* ``registry``   parsers keyed by media type, plus the ``EInvoiceParser`` registry.
* ``pdf``        pdfplumber (layout, chars, colours) + pypdf (metadata, XMP, structure).
* ``docx``       python-docx (runs with font, size, colour, hidden, core properties).
* ``ocr``        Tesseract (eng + ara) for images and scanned PDF pages.
* ``text``       plain text (email bodies).
* ``vision``     the ``VisionFallback`` interface for low-confidence OCR.
* ``sandbox``    runs a parser in a separate, resource-limited subprocess.
* ``pipeline``   the ``parse_document`` job: sandboxed parse, store result, set status.
* ``permissions``  document permissions and the per-workflow authorisation rule.

No PyMuPDF (AGPL): see docs and the license gate.
"""
