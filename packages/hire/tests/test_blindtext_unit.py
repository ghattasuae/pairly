# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Blind mode helpers for free text and the approver's view of a message (pure)."""

from __future__ import annotations

from pairly_hire.blindtext import (
    EMAIL_HIDDEN,
    NAME_HIDDEN,
    PHONE_HIDDEN,
    identity_sources,
    mask_identity,
    mask_values,
    redact_free_text,
    redact_values,
    redactor,
)
from pairly_hire.screening.profile import Located, ParsedProfile

CONTACT = {
    "full_name": "Layla Haddad",
    "email": "layla.h@example.test",
    "phone": "+971 50 123 4567",
    "location": "Dubai",
}


def test_mask_identity_keeps_the_text_and_labels_what_it_hides() -> None:
    sources = identity_sources(CONTACT)
    text = "Dear Layla Haddad,\nCall +971 50 123 4567 or write to LAYLA.H@example.test. Layla"
    assert mask_identity(text, sources) == (
        f"Dear {NAME_HIDDEN},\nCall {PHONE_HIDDEN} or write to {EMAIL_HIDDEN}. {NAME_HIDDEN}"
    )
    # Nothing known: unchanged, exactly.
    assert mask_identity("We wish you well.", sources) == "We wish you well."


def test_mask_values_touches_only_the_given_keys() -> None:
    sources = identity_sources(CONTACT)
    payload = {"text": "Dear Layla", "recipient": "email:layla.h@example.test", "kind": "Layla"}
    masked = mask_values(payload, sources, ("text", "recipient"))
    assert masked == {
        "text": f"Dear {NAME_HIDDEN}",
        "recipient": f"email:{EMAIL_HIDDEN}",
        "kind": "Layla",
    }


def test_free_text_redaction_uses_every_known_identity_token() -> None:
    profile = ParsedProfile(name=Located("L. Haddad-Saleh"), city=Located("Sharjah"))
    sources = identity_sources(CONTACT, profile=profile)
    note = "Saleh from Sharjah (layla.h@example.test) moved to Dubai; she is keen."
    assert redact_free_text(note, sources) == (
        "[name] from [address] ([contact]) moved to [address]; [gender] is keen."
    )
    run = redactor(sources)
    assert redact_values({"a": ["Layla"], "b": 3}, run) == {"a": ["[name]"], "b": 3}
    assert run("") == ""
