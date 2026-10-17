# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""The candidate portal (brief 3.9, WP2.9).

* ``auth``       magic links and portal sessions; the candidate actor; RLS binding.
* ``status``     the honest, candidate-facing steps of an application.
* ``service``    what a candidate sees and does: applications, messages, files,
                 withdrawal, open jobs, the privacy centre.
* ``models``     ``hire_portal_tokens``, ``hire_portal_sessions``, ``hire_data_requests``,
                 ``hire_document_requests`` (migration ``hire_0004b``).
* ``templates``  the magic link and withdrawal confirmation emails.

Never imports the self-identification answers (import-linter contract).
"""
