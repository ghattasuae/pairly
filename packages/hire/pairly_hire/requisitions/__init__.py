# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Requisitions (brief 3.1.1): a hiring manager requests a role; an approval chain
(``approval_rules.yaml``) approves it before a job can be opened from it.

* ``models``: ``hire_requisitions`` (migration ``hire_0008``).
* ``service``: create, list, cancel, open a job from an approved requisition.
* ``graph``: the ``hire_requisition`` approval graph and its final action.
"""
