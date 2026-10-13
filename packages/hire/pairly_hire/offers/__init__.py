# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Offers (brief 3.8.3, M3). No e-signature integration (ASK open): an offer is a PDF
plus the candidate's accept or decline in the portal, audited.

* ``templates``: the offer templates (compensation fields, default conditions, the
  letter's wording in English and Arabic).
* ``models``: ``hire_offers`` (compensation encrypted).
* ``service``: build and edit, submit for approval, send after approval, the
  candidate's answer (a decision record), withdraw; who sees compensation.
* ``graph`` with ``approval_rules.yaml``: hiring manager, then HR, and finance above a
  threshold; the submitter never approves.
* ``pdf``: the offer letter as a PDF, English and Arabic.
"""
