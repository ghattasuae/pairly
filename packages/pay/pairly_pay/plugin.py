# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Placeholder Pairly Pay plugin, registered in the ``pairly.workflows`` entry point group.

The full plugin (models, graphs, detectors, rules, router) is built in later work packages.
"""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PayPlugin:
    """Minimal plugin satisfying ``pairly_core.plugins.WorkflowPlugin``."""

    id: str = "pay"
    name: str = "Pairly Pay"


plugin = PayPlugin()
