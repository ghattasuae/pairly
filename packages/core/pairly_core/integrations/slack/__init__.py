# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Slack: request signatures, Block Kit approval messages, interactions, account links.

* ``signature``  Slack request signing (v0 HMAC-SHA256, 5 minute window).
* ``blocks``     Block Kit messages.
* ``transport``  ``SlackTransport`` (httpx client and a fake).
* ``service``    the integration row, the signed connect flow and interaction handling.

Why a Slack click never approves (documented design choice): approving or rejecting
needs the approval permission, and every approval permission is a step-up permission
(TOTP in the current sign-in, ``pairly_core.rbac``). Slack cannot prove a second factor.
So the Approve and Reject buttons are link buttons that open the web confirmation page
(``/approvals/link#<token>``) with a signed single-use link token bound to the person;
the web page needs a TOTP sign-in and an explicit click, and decides through the same
``decide`` as the inbox. The click event Slack also sends us is only acknowledged and
audited. Only actions that need no step-up run natively in Slack (``remind_later``).
Every action payload carries a signed link token, never a bare approval id, and the
token's bound user must be the Pairly user the Slack user verified as.
"""
