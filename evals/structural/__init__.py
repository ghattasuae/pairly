# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Structural evals of the AI components no other eval covers (``python -m evals.structural``).

Read from the committed recordings (the scripted answers the demo and the offline tests
replay, and the live recordings when a task has any): no model call, no new data.
For each component, on every recorded output of its task:

* ``schema_valid``: the output validates against the component's output schema (the
  router's acceptance rule). Gated at 1.0.
* ``banned_content_free``: no string of the output trips the router's default
  banned-content policy (markup, images, data URIs, prompt leaks, claims of having
  acted). Gated at 1.0.
* ``em_dash_free``: no em dash anywhere in the output.
* ``product_check``: the component's own deterministic check, the one the product runs
  before showing a draft (inclusive language on a drafted job description, "no invented
  facts" on a personalised message, the kit, debrief, reference, clarification and note
  checks, codes from the lists shown for a GL coding).
* ``references_exist``: where an output points back at its input (a debrief's criterion
  ids, a coding's codes, a span's quote), every pointer exists in the request.
* ``schema_current``: the recording was made against today's schema (its hash).
* ``size``: request and response sizes in characters and the prompt file's size.
* ``cost``: only from live recordings (tokens and USD as measured); without one, not
  measured.

These are structural checks, not quality judgments: scripted answers are not model
output, so nothing here says how good a draft is.
"""
