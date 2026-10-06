# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Placeholder Pairly Hire plugin, registered in the ``pairly.workflows`` entry point group.

The full plugin (models, graphs, detectors, rules, router) is built in later work packages.
"""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class HirePlugin:
    """Minimal plugin satisfying ``pairly_core.plugins.WorkflowPlugin``."""

    id: str = "hire"
    name: str = "Pairly Hire"


plugin = HirePlugin()
