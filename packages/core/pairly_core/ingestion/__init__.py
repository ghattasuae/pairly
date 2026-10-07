# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Ingestion: every way a file enters Pairly (upload, API, inbound email, forms).

The path is the same for every source:

1. Spool the bytes to a private temp file, hashing (SHA-256) and counting while they
   arrive. Stop early when the workflow's size limit is passed.
2. Sniff the media type from the bytes (never trust the client's type or file name).
3. Check the workflow's intake policy (allowed types, maximum size). Reject early.
4. Dedupe by ``(org_id, sha256)``: a known file returns the existing document id.
5. Store the bytes in object storage under ``orgs/<org>/blobs/sha256/<hash>``, never
   under a user-supplied name.
6. Insert the ``documents`` row (status ``received``), write an audit event, and queue
   the virus scan. Parsing only starts after a clean scan.

This package is imported lazily by the API and the worker; importing it does not
contact any service. Submodules: ``settings``, ``sniff``, ``storage``, ``models``,
``service``, ``email``, ``scan``, ``queue``.
"""
