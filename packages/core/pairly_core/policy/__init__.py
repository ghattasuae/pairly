# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Policy engine (WP1.5): deterministic rules plus checks against tenant policy documents.

* ``pairly_core.policy.expr``: the safe expression language for rule conditions.
* ``pairly_core.policy.rules``: rule set YAML (Pydantic) and pure evaluation.
* ``pairly_core.policy.service``: publish, load, evaluate and record rule sets.
* ``pairly_core.policy.documents``: policy documents, chunking and indexing.
* ``pairly_core.policy.rag``: ``check_against_policies`` with verified citations.

Kept free of imports here: ``pairly_core.rbac`` loads ``policy.permissions`` lazily.
"""
