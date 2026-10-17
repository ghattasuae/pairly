# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Batch document submission (``POST /v1/documents/batch``, M6).

* ``models``   ``document_batches`` and ``document_batch_items``.
* ``service``  create, record item outcomes, complete (``batch.completed``), read.
* ``intake``   the optional ``document_intake`` a workflow declares to turn a submitted
  document into its own entity (Pairly Pay: an invoice and its run).

URLs are fetched with ``pairly_core.ingestion.fetch`` (SSRF protected).
"""
