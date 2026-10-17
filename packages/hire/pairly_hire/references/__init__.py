# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Reference checks (brief 3.8.2 and 5.1.6, M3).

* ``models``: reference requests (the referee's name and address, their answers, all
  encrypted), single use links, referee sessions and kept summaries.
* ``form``: the structured reference form, English and Arabic, and its checks.
* ``links``: the referee's signed single use link and the short session it opens.
* ``service``: request, send, save progress, submit, cancel; every step audited.
* ``consistency``: deterministic flags comparing what referees said with the CV.
* ``summary``: the summary for the hiring team through the model router (recorded for
  the demo), with a deterministic fallback; blind safe.
"""
