# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Proposals: an agent or an API client suggests, a person decides (M6).

* ``models``       the ``proposals`` table (org scoped, RLS forced, append only).
* ``permissions``  ``core.proposal.propose`` and ``core.proposal.read``.
* ``service``      create and list proposals.

A proposal is a note, or a suggested action on one entity ("approve this invoice",
"move this candidate to interview"). It changes nothing else: no approval is decided,
no payment made, nothing deleted. People see proposals next to the entity and act
through their own permissions. This is how the MCP server's ``propose_action`` and API
keys with a ``propose`` scope hand work to people.
"""
