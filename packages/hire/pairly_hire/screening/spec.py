# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""The ``hire_screening`` graph as the plugin declares it to the runtime.

Started by the agent (``hire.screening.propose``) when an application is submitted, or
when a person asks for a new screening. Read with ``hire.screening.read``. Its one
approval type, ``hire.screening``, is decided with ``hire.screening.approve``; its one
final action is ``shortlist``.
"""

from __future__ import annotations

from importlib.resources import files
from typing import Final

from pairly_core.workflow.spec import ApprovalType, GraphSpec
from pairly_hire.screening.actions import SHORTLIST, shortlist
from pairly_hire.screening.graph import APPROVAL_TYPE, GRAPH, VERSION, build_graph
from pairly_hire.screening.permissions import (
    SCREENING_APPROVE,
    SCREENING_PROPOSE,
    SCREENING_READ,
)

APPROVAL_RULES_V1: Final = (
    files("pairly_hire").joinpath("screening", "policy", "approval_rules.yaml").read_text("utf-8")
)

HIRE_SCREENING: Final = GraphSpec(
    workflow="hire",
    name=GRAPH,
    version=VERSION,
    build=build_graph,
    start_permission=SCREENING_PROPOSE,
    read_permission=SCREENING_READ,
    approval_types=(
        ApprovalType(APPROVAL_TYPE, SCREENING_APPROVE, APPROVAL_RULES_V1, "Screening"),
    ),
    final_actions={SHORTLIST: shortlist},
    description="Application to shortlist decision: read, check, score, and a person decides.",
)
