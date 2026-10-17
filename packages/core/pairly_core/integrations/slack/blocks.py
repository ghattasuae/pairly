# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Block Kit messages.

Approval messages carry three buttons:

* ``approve`` and ``reject``: link buttons. Their ``url`` opens the web confirmation
  page (``/approvals/link#<token>``) and their ``value`` is the same signed single-use
  link token. Slack also posts the click to our interaction endpoint, which only
  acknowledges it: the decision is made on the web page, behind a TOTP sign-in.
* ``remind_later``: a native action (no step-up needed), whose ``value`` is a link
  token of the same person, never a bare approval id.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final

APPROVE: Final = "approve"
REJECT: Final = "reject"
REMIND_LATER: Final = "remind_later"
APPROVAL_BLOCK_ID: Final = "pairly_approval"

# Actions that run in Slack itself. Everything else opens the web app.
NATIVE_ACTIONS: Final = frozenset({REMIND_LATER})
# Actions that need the web confirmation page (step-up: TOTP sign-in).
WEB_ACTIONS: Final = frozenset({APPROVE, REJECT})

LABELS: Final[dict[str, dict[str, str]]] = {
    "en": {
        APPROVE: "Approve",
        REJECT: "Reject",
        REMIND_LATER: "Remind me in 1 hour",
        "note": "Approve and Reject open Pairly, where you confirm after a two-factor sign-in.",
    },
    "ar": {
        APPROVE: "موافقة",
        REJECT: "رفض",
        REMIND_LATER: "ذكرني بعد ساعة",
        "note": "زرا الموافقة والرفض يفتحان Pairly لتأكيد قرارك بعد تسجيل الدخول بالتحقق الثنائي.",
    },
}


@dataclass(frozen=True, slots=True)
class SlackButton:
    action_id: str
    label: str
    value: str
    url: str | None = None
    style: str | None = None  # "primary" | "danger"

    def to_block(self) -> dict[str, Any]:
        element: dict[str, Any] = {
            "type": "button",
            "action_id": self.action_id,
            "text": {"type": "plain_text", "text": self.label[:75], "emoji": False},
            "value": self.value[:2000],
        }
        if self.url:
            element["url"] = self.url[:3000]
        if self.style in {"primary", "danger"}:
            element["style"] = self.style
        return element


def message_blocks(
    text: str, buttons: Sequence[SlackButton] = (), *, note: str | None = None
) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": text[:3000]}}
    ]
    if buttons:
        blocks.append(
            {
                "type": "actions",
                "block_id": APPROVAL_BLOCK_ID,
                "elements": [b.to_block() for b in buttons],
            }
        )
    if note:
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": note[:3000]}]})
    return blocks


def approval_buttons(
    *,
    locale: str,
    approve_url: str,
    approve_token: str,
    reject_url: str,
    reject_token: str,
) -> tuple[list[SlackButton], str]:
    labels = LABELS.get(locale, LABELS["en"])
    buttons = [
        SlackButton(APPROVE, labels[APPROVE], approve_token, url=approve_url, style="primary"),
        SlackButton(REJECT, labels[REJECT], reject_token, url=reject_url, style="danger"),
        SlackButton(REMIND_LATER, labels[REMIND_LATER], approve_token),
    ]
    return buttons, labels["note"]
