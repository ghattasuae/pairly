# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Unified search over every searchable kind (WP1.12).

* ``sources``    the search source registry: one ``SearchSource`` per kind (``policy``,
                 ``document``; workflow plugins add theirs, such as candidates or
                 invoices, through an optional ``search_sources`` attribute). A source
                 says who may search it, which of its sources a caller may see, how to
                 describe a hit, and which feature flag (if any) must be on.
* ``documents``  indexing parsed documents (kind ``document``): visible text only, PII
                 redacted before anything is stored or embedded, only while the
                 organisation's ``search.documents`` flag is on.
* ``service``    ``search``: runs the hybrid search (``pairly_core.retrieval.search``)
                 for each kind the caller may search and merges the hits by score.

Access is checked twice: per kind (the caller's permission, plus the flag), then per
source (for documents, only the workflows whose documents the caller may read). Row
Level Security still limits everything to the organisation.
"""
