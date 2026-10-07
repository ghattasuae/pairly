# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Workflow runtime: LangGraph graphs with a tenant-bound Postgres checkpointer.

Modules:

* ``spec``: what a workflow plugin declares (``GraphSpec``, ``ApprovalType``, final actions).
* ``models``: the ``workflow_runs`` table.
* ``checkpoint``: the tenant-bound LangGraph checkpointer (see its docstring for how
  checkpoints stay inside one organisation).
* ``runtime``: start, resume (only after a human decision) and cancel runs.
* ``nodes``: building blocks for graph nodes (request an approval, wait for it, run the
  final action through the executor).

This package imports nothing on purpose, so ``pairly_core.models`` can import the table
definitions without import cycles.
"""
