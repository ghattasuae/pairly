# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""The pre-employment checklist (brief 3.8.4, M3).

* ``models``: the documents to collect per job (configurable, with the retention in
  days after hire) and one checklist item per document and application.
* ``service``: request the checklist through the candidate portal's file requests,
  store each file encrypted (envelope encryption, its own object key), track status
  (requested, received, verified, waived), read a file (HR and admins only, audited),
  and the retention: ``retention_until`` is set once the candidate is hired
  (default 90 days after), counted by the ``hire.checklist.documents`` retention rule.
"""
