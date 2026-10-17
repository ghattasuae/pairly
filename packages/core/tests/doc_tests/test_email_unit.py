# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Inbound email parsing and addressing, on synthetic messages built in the tests."""

from __future__ import annotations

from email.message import EmailMessage
from email.policy import SMTP

import pytest

from pairly_core.ingestion.addresses import (
    MAX_ROUTE_CANDIDATES,
    InboundRoute,
    candidate_routes,
    hash_inbound_token,
    inbound_address,
    new_inbound_token,
    parse_inbound_address,
)
from pairly_core.ingestion.email import html_to_text, parse_email

from . import doc_fixtures as fx

DOMAIN = "inbound.pairly.localhost"
PDF = fx.simple_pdf()


def _raw(msg: EmailMessage) -> bytes:
    return msg.as_bytes(policy=SMTP)


TOKEN = "k3m7q2xv7hb4tnwe"  # noqa: S105 (synthetic test token)


def test_tokens() -> None:
    tokens = {new_inbound_token() for _ in range(50)}
    assert len(tokens) == 50
    for token in tokens:
        assert len(token) == 16
        assert set(token) <= set("abcdefghijklmnopqrstuvwxyz234567")
    digest = hash_inbound_token(TOKEN)
    assert len(digest) == 64
    assert digest != hash_inbound_token(new_inbound_token())
    for bad in ("short", "UPPERCASE1234567", "has.dot.in.it.xx", ""):
        with pytest.raises(ValueError, match="invalid inbound token"):
            hash_inbound_token(bad)


def test_addresses() -> None:
    assert (
        inbound_address("pay", "acme", TOKEN, "Inbound.Example.COM")
        == f"pay+acme.{TOKEN}@inbound.example.com"
    )
    for args in (("Pay", "acme", TOKEN), ("pay", "acme", "short"), ("pay", "a.b", TOKEN)):
        with pytest.raises(ValueError, match="invalid"):
            inbound_address(*args, DOMAIN)
    route = parse_inbound_address(
        f"Acme AP <PAY+Acme-Ltd.{TOKEN.upper()}@{DOMAIN.upper()}>", DOMAIN
    )
    assert route == InboundRoute("pay", "acme-ltd", TOKEN)
    # The token never shows up in a repr (logs, tracebacks).
    assert TOKEN not in repr(route)
    for bad in (
        f"pay@{DOMAIN}",
        f"pay+acme@{DOMAIN}",  # no token: the slug alone never routes
        f"pay+acme.@{DOMAIN}",
        f"pay+acme.short@{DOMAIN}",
        f"pay+acme.{TOKEN}1@{DOMAIN}",  # 1 is not base32
        f"pay+.{TOKEN}@{DOMAIN}",
        f"pay+acme.{TOKEN}@other.example",
        f"pay+a.{TOKEN}@{DOMAIN}",
        f"9pay+acme.{TOKEN}@{DOMAIN}",
        f"pay+-acme.{TOKEN}@{DOMAIN}",
        "",
        "not an address",
    ):
        assert parse_inbound_address(bad, DOMAIN) is None, bad


def test_candidate_routes() -> None:
    workflows = frozenset({"pay", "hire"})
    hits = candidate_routes(
        [
            "someone@example.test",
            f"toy+acme.{TOKEN}@{DOMAIN}",
            f"hire+acme.{TOKEN}@{DOMAIN}",
            f"Hire+Acme.{TOKEN}@{DOMAIN}",
            f"pay+acme.{TOKEN}@{DOMAIN}",
        ],
        DOMAIN,
        workflows,
    )
    assert [h.workflow for h in hits] == ["hire", "pay"]
    assert candidate_routes([f"toy+acme.{TOKEN}@{DOMAIN}"], DOMAIN, workflows) == []
    many = [f"pay+org-{i}.{TOKEN}@{DOMAIN}" for i in range(20)]
    assert len(candidate_routes(many, DOMAIN, workflows)) == MAX_ROUTE_CANDIDATES


def test_attachments_sender_and_headers() -> None:
    msg = fx.email_with_attachments(
        to=f"pay+acme@{DOMAIN}",
        attachments=(
            ("invoice.pdf", PDF, "application/pdf"),
            ("cv.docx", b"PK\x03\x04docx", "application/octet-stream"),
        ),
        extra_headers={"Reply-To": "Accounts <accounts@vendor.example>", "Cc": "x@vendor.example"},
    )
    parsed = parse_email(_raw(msg))
    assert parsed.message_id == "synthetic-1@vendor.example"
    assert parsed.sender == "billing@vendor.example"
    assert parsed.sender_name == "Billing Team"
    assert parsed.sender_domain == "vendor.example"
    assert parsed.reply_to == "accounts@vendor.example"
    assert f"pay+acme@{DOMAIN}" in parsed.recipients
    assert parsed.subject == "Invoice INV-0001"
    assert parsed.sent_at is not None
    assert parsed.sent_at.utcoffset() is not None
    assert parsed.text_body == "Please find the invoice attached."
    assert [(a.filename, a.data[:4]) for a in parsed.attachments] == [
        ("invoice.pdf", PDF[:4]),
        ("cv.docx", b"PK\x03\x04"),
    ]
    assert parsed.attachments[1].declared_type == "application/octet-stream"
    assert not parsed.is_bulk
    assert not parsed.malformed


def test_html_only_body_and_inline_logo() -> None:
    msg = EmailMessage()
    msg["From"] = "vendor@vendor.example"
    msg["To"] = f"pay+acme@{DOMAIN}"
    msg["Subject"] = "Invoice in body"
    msg.add_alternative(
        "<html><head><style>p{}</style><script>x()</script></head><body>"
        "<p>Invoice&nbsp;INV-7</p><table><tr><td>Total</td><td>50.00</td></tr></table>"
        "</body></html>",
        subtype="html",
    )
    msg.get_payload()[0].add_related(  # type: ignore[index, union-attr]
        fx.text_png(), maintype="image", subtype="png", cid="<logo@vendor.example>"
    )
    parsed = parse_email(_raw(msg))
    assert "Invoice INV-7" in parsed.text_body
    assert "Total" in parsed.text_body
    assert "x()" not in parsed.text_body
    assert parsed.html_body is not None
    assert parsed.attachments == []
    assert parsed.skipped_inline_images == 1
    # A missing Message-ID gets a stable synthetic one from the raw hash.
    assert parsed.message_id == f"{parsed.raw_sha256}@pairly.invalid"


def test_forwarded_as_attachment_keeps_inner_files() -> None:
    inner = fx.email_with_attachments(
        sender="Original Vendor <ap@original.example>",
        subject="Original invoice",
        message_id="<inner-1@original.example>",
        attachments=(("original.pdf", PDF, "application/pdf"),),
    )
    outer = fx.email_with_attachments(
        sender="Clerk <clerk@customer.example>",
        subject="Fwd: Original invoice",
        message_id="<outer-1@customer.example>",
        body="FYI",
    )
    outer.add_attachment(inner)
    parsed = parse_email(_raw(outer))
    assert [a.filename for a in parsed.attachments] == ["original.pdf"]
    assert parsed.attachments[0].depth == 1
    (forward,) = parsed.forwarded
    assert forward.kind == "attached"
    assert forward.sender == "ap@original.example"
    assert forward.subject == "Original invoice"
    assert forward.message_id == "inner-1@original.example"


@pytest.mark.parametrize(
    "marker",
    [
        "---------- Forwarded message ---------",
        "Begin forwarded message:",
        "-----Original Message-----",
        "> ---------- Forwarded message ---------",
    ],
)
def test_inline_forward_chains(marker: str) -> None:
    prefix = "> " if marker.startswith(">") else ""
    body = (
        "See below.\n\n"
        f"{marker}\n"
        f"{prefix}From: Original Vendor <ap@original.example>\n"
        f"{prefix}Date: Mon, 5 Oct 2026 at 10:00\n"
        f"{prefix}Subject: Invoice 99\n"
        f"{prefix}To: clerk@customer.example\n\n"
        f"{prefix}Please pay invoice 99.\n"
    )
    parsed = parse_email(_raw(fx.email_with_attachments(body=body)))
    (forward,) = parsed.forwarded
    assert forward.kind == "inline"
    assert forward.sender == "ap@original.example"
    assert forward.subject == "Invoice 99"
    assert forward.date is not None


def test_marker_without_headers_is_ignored() -> None:
    body = "Notes\n________________________________\nnothing that looks like headers\n"
    assert parse_email(_raw(fx.email_with_attachments(body=body))).forwarded == []


@pytest.mark.parametrize(
    "headers",
    [
        {"List-Unsubscribe": "<mailto:unsub@news.example>"},
        {"Precedence": "bulk"},
        {"Auto-Submitted": "auto-generated"},
        {"List-Id": "<news.example>"},
    ],
)
def test_bulk_hints(headers: dict[str, str]) -> None:
    assert parse_email(_raw(fx.email_with_attachments(extra_headers=headers))).is_bulk


def test_malformed_input_never_raises() -> None:
    for raw in (b"", b"\x00\xff\xfe garbage", b"From: \r\nSubject: =?bad?=\r\n\r\nbody"):
        parsed = parse_email(raw)
        assert parsed.message_id.endswith("@pairly.invalid")
        assert parsed.attachments == []
    weird = b"From: not-an-address\r\nDate: yesterday\r\nMessage-ID: <x@y>\r\n\r\nhello"
    parsed = parse_email(weird)
    assert parsed.sender is None
    assert parsed.sent_at is None
    assert parsed.text_body == "hello"


def test_deep_nesting_is_bounded() -> None:
    boundary_parts = []
    depth = 40
    for i in range(depth):
        boundary_parts.append(f'Content-Type: multipart/mixed; boundary="b{i}"\r\n\r\n--b{i}\r\n')
    raw = (
        b"From: a@vendor.example\r\nMIME-Version: 1.0\r\n"
        + "".join(boundary_parts).encode()
        + b"Content-Type: application/pdf\r\n"
        + b"Content-Disposition: attachment; filename=x.pdf\r\n\r\n"
        + b"%PDF-1.4\r\n"
    )
    parsed = parse_email(raw)
    assert parsed.malformed


def test_html_to_text() -> None:
    assert html_to_text("<p>a</p><p>b</p>") == "a\n\nb"
    assert html_to_text("<div>x<br>y</div>") == "x\ny"
    assert html_to_text("<script>bad()</script>ok") == "ok"
