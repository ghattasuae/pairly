# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""The candidate experience survey (brief 3.8.5, M3).

* ``models``: survey invites (one per finished application) and responses, kept
  apart so a response cannot be traced back to an invite.
* ``form``: the 5 questions, English and Arabic.
* ``service``: invite after every outcome (hired, rejected once it was sent,
  withdrawn) with a signed single use link, answer anonymously by default (the
  candidate may attach their name), and ``aggregate_results`` for reports and the
  analytics agent (counts and averages only; small groups suppressed).
* ``templates``: the invitation email, English and Arabic.
"""
