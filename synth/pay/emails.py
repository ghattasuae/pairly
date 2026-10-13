# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Inbound emails to the AP inbox, written as RFC 822 ``.eml`` files at generate time.

Every invoice whose channel is ``email`` arrives in one. Kinds:

* ``single``           one invoice attached.
* ``multi_attachment`` two or three invoices from one vendor in one email (the split
                       invoice groups always arrive this way).
* ``with_extra``       an invoice plus a non-invoice attachment (terms, a logo).
* ``body``             the invoice is the email body text (no attachment).
* ``forwarded``        a colleague forwards the vendor's email to the AP inbox; the
                       vendor's address is only in the forwarded header block.
* ``marketing``        newsletters and offers with no invoice: to ignore.
* ``demo``             the three Pay demo emails (forwarded, brief 10.2).

Headers, dates, message ids and MIME boundaries are fixed, so the bytes are the same on
every run. Sender domains are under ``.example`` or ``.test`` (RFC 2606), so no address
can reach anyone.
"""

from __future__ import annotations

import datetime as dt
import io
import random
from email.message import EmailMessage
from email.policy import SMTP
from email.utils import format_datetime
from typing import Final, cast

from reportlab.pdfgen import canvas  # type: ignore[import-untyped]

from synth.hire.facts import stable_seed
from synth.pay.catalog import AP_INBOX, BUYER_DOMAIN, FIRST_NAMES, LAST_NAMES
from synth.pay.dataset import Dataset, DocSpec
from synth.pay.models import Attachment, EmailRecord
from synth.pay.render import render_body

TZ: Final = dt.timezone(dt.timedelta(hours=4))
CONTENT_TYPES: Final = {
    "pdf": "application/pdf",
    "png": "image/png",
    "jpg": "image/jpeg",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xml": "application/xml",
    "txt": "text/plain",
}
SENDER_FINDINGS: Final = frozenset(
    {"sender_lookalike_domain", "sender_domain_mismatch", "first_time_sender", "unknown_vendor"}
)
STAFF: Final = f"finance.ops@{BUYER_DOMAIN}"
MARKETING: Final = (
    ("news@{d}", "Our autumn catalogue is here", "See what is new this season.", True),
    ("offers@{d}", "Save 15% on annual plans", "Upgrade before the end of the month.", False),
    ("events@{d}", "Invitation: supplier day 2026", "Join us for our supplier day.", False),
    ("news@{d}", "Product update: September", "Here is what changed this month.", True),
    (
        "hello@promo-mailer.example",
        "You have been selected for a business award",
        "Claim your listing today.",
        False,
    ),
    (
        "newsletter@gulf-trade-digest.example",
        "Weekly trade digest",
        "This week's headlines for importers.",
        False,
    ),
    ("webinar@{d}", "Webinar: AP automation trends", "Register for free.", False),
    ("info@{d}", "Holiday opening hours", "Our offices will close early on Thursday.", False),
)
EXTRA_ATTACHMENTS: Final = ("Terms_and_conditions.pdf", "company_logo.png")


def _ext(file: str) -> str:
    return file.rsplit(".", 1)[-1]


def _name(rng: random.Random) -> str:
    return f"{rng.choice(FIRST_NAMES)} {rng.choice(LAST_NAMES)}"


def _sender(spec: DocSpec) -> str:
    info = spec.record.attack
    if info is not None and "sender" in info.notes:
        return info.notes["sender"]
    return spec.doc.vendor.sender_addresses[0]


def _findings(specs: list[DocSpec]) -> tuple[str, ...]:
    found: set[str] = set()
    for spec in specs:
        if spec.record.attack is not None:
            found.update(f for f in spec.record.attack.expected_findings if f in SENDER_FINDINGS)
    return tuple(sorted(found))


def _when(spec: DocSpec, rng: random.Random) -> str:
    day = dt.date.fromisoformat(spec.record.truth.invoice_date) + dt.timedelta(
        days=rng.randint(0, 2)
    )
    moment = dt.datetime(
        day.year, day.month, day.day, rng.randint(8, 17), rng.randint(0, 59), tzinfo=TZ
    )
    return moment.isoformat()


def _attachment(spec: DocSpec) -> Attachment:
    """A vendor-style file name (never the dataset id, which would give attacks away)."""
    number = "".join(c if c.isalnum() else "_" for c in spec.record.truth.invoice_number)
    filename = f"Invoice_{number.strip('_')}.{_ext(spec.record.file)}"
    return Attachment(
        filename=filename, content_type=CONTENT_TYPES[_ext(filename)], invoice_id=spec.record.id
    )


def _subject(specs: list[DocSpec]) -> str:
    numbers = ", ".join(s.record.truth.invoice_number for s in specs)
    word = "Invoices" if len(specs) > 1 else "Invoice"
    if specs[0].record.language == "ar":
        return f"فاتورة {numbers}"
    return f"{word} {numbers} from {specs[0].doc.vendor.trade_name}"


def build_emails(ds: Dataset) -> None:
    rng = random.Random(stable_seed(ds.seed, "emails"))
    emails: list[EmailRecord] = []
    assigned: dict[str, str] = {}

    def add(kind: str, specs: list[DocSpec], **kw: object) -> None:
        eid = f"eml-{len(emails) + 1:04d}"
        attachments = cast(
            "list[Attachment]", kw.pop("attachments", [_attachment(s) for s in specs])
        )
        sender = str(kw.pop("sender", _sender(specs[0]) if specs else ""))
        record = EmailRecord(
            id=eid,
            file=f"emails/{eid}.eml",
            kind=kind,
            sender=sender,
            original_sender=kw.pop("original_sender", None),  # type: ignore[arg-type]
            to=AP_INBOX,
            subject=str(kw.pop("subject", _subject(specs) if specs else "")),
            date=str(kw.pop("date", _when(specs[0], rng) if specs else "")),
            attachments=tuple(attachments),
            body_invoice_id=kw.pop("body_invoice_id", None),  # type: ignore[arg-type]
            invoice_ids=tuple(s.record.id for s in specs),
            ignore=bool(kw.pop("ignore", False)),
            demo=kw.pop("demo", None),  # type: ignore[arg-type]
            expected_findings=_findings(specs),
        )
        if kw:
            raise TypeError(kw)
        emails.append(record)
        for s in specs:
            assigned[s.record.id] = eid

    # Demo: three forwarded emails, in demo order.
    for key in ("clean", "near_duplicate", "bank_change"):
        spec = next(s for s in ds.docs if s.record.demo == key)
        add(
            "demo",
            [spec],
            sender=STAFF,
            original_sender=_sender(spec),
            demo=key,
            subject=f"Fwd: {_subject([spec])}",
            date=f"2026-09-30T09:{10 + len(emails):02d}:00+04:00",
        )

    # Split invoice groups arrive together.
    groups: dict[str, list[DocSpec]] = {}
    for spec in ds.docs:
        info = spec.record.attack
        if info is not None and "group" in info.notes:
            groups.setdefault(info.notes["group"], []).append(spec)
    for specs in groups.values():
        add("multi_attachment", specs)

    pending = [s for s in ds.docs if s.record.channel == "email" and s.record.id not in assigned]
    i = 0
    while i < len(pending):
        spec = pending[i]
        if spec.record.format == "email_body":
            add("body", [spec], attachments=[], body_invoice_id=spec.record.id)
            i += 1
            continue
        roll = rng.random()
        nxt = pending[i + 1] if i + 1 < len(pending) else None
        if (
            roll < 0.12
            and spec.record.kind == "invoice"
            and nxt is not None
            and nxt.record.kind == "invoice"
            and nxt.doc.vendor.id == spec.doc.vendor.id
            and nxt.record.format != "email_body"
        ):
            add("multi_attachment", [spec, nxt])
            i += 2
            continue
        if roll < 0.22 and spec.record.kind == "invoice":
            add(
                "forwarded",
                [spec],
                sender=STAFF,
                original_sender=_sender(spec),
                subject=f"Fwd: {_subject([spec])}",
            )
        elif roll < 0.32:
            extra = rng.choice(EXTRA_ATTACHMENTS)
            atts = [
                _attachment(spec),
                Attachment(
                    filename=extra, content_type=CONTENT_TYPES[_ext(extra)], invoice_id=None
                ),
            ]
            add("with_extra", [spec], attachments=atts)
        else:
            add("single", [spec])
        i += 1

    # Marketing: nothing to extract.
    for k, (sender, subject, _body, brochure) in enumerate(MARKETING):
        vendor = ds.vendors[(k * 7) % len(ds.vendors)]
        atts = (
            [Attachment(filename="catalogue.pdf", content_type="application/pdf", invoice_id=None)]
            if brochure
            else []
        )
        add(
            "marketing",
            [],
            sender=sender.format(d=vendor.email_domains[0]),
            subject=subject,
            date=f"2026-09-{3 + 3 * k:02d}T10:15:00+04:00",
            attachments=atts,
            ignore=True,
        )

    ds.emails = emails
    for n, spec in enumerate(ds.docs):
        eid = assigned.get(spec.record.id)
        if eid is None:
            continue
        update: dict[str, object] = {"email_id": eid}
        if spec.record.format == "email_body":
            update["file"] = f"emails/{eid}.eml"
        ds.docs[n] = DocSpec(spec.record.model_copy(update=update), spec.doc)


# ---------------------------------------------------------------------- rendering


def _greeting(spec: DocSpec | None, rng: random.Random) -> str:
    if spec is None:
        return ""
    if spec.record.language == "ar":
        return "السادة الكرام،\n\nمرفق الفاتورة. شكراً لكم.\n"
    contact = spec.doc.vendor.contacts[0].name
    lines = [
        "Dear Accounts Payable team,",
        "",
        rng.choice(
            (
                "Please find our invoice attached.",
                "Attached is our invoice for your records and payment.",
                "Kindly find the attached invoice.",
            )
        ),
    ]
    for note in spec.doc.notes:
        if spec.record.attack is not None:
            lines.append(note)
    lines += ["", "Best regards,", contact, spec.doc.vendor.legal_name]
    return "\n".join(lines) + "\n"


def _small_pdf(title: str) -> bytes:
    buf = io.BytesIO()
    pdf = canvas.Canvas(buf, invariant=1)
    pdf.setTitle(title)
    pdf.setFont("Helvetica", 12)
    pdf.drawString(72, 760, title)
    pdf.drawString(72, 740, "This document is not an invoice.")
    pdf.showPage()
    pdf.save()
    return buf.getvalue()


def _logo_png() -> bytes:
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (64, 64), (255, 255, 255))
    ImageDraw.Draw(image).ellipse((8, 8, 56, 56), fill=(31, 58, 104))
    buf = io.BytesIO()
    image.save(buf, format="PNG", optimize=False)
    return buf.getvalue()


def render_email(record: EmailRecord, ds: Dataset, blobs: dict[str, bytes]) -> bytes:
    """The ``.eml`` bytes; ``blobs`` holds each invoice's rendered file by id."""
    rng = random.Random(stable_seed(ds.seed, "email-body", record.id))
    specs = [ds.doc(i) for i in record.invoice_ids]
    first = specs[0] if specs else None
    msg = EmailMessage(policy=SMTP)
    msg["From"] = record.sender
    msg["To"] = record.to
    msg["Subject"] = record.subject
    msg["Date"] = format_datetime(dt.datetime.fromisoformat(record.date))
    domain = record.sender.rsplit("@", 1)[-1]
    msg["Message-ID"] = f"<{record.id}.{ds.seed}@{domain}>"
    if record.kind == "marketing":
        body = next(b for s, subj, b, _x in MARKETING if subj == record.subject)
        body = f"{body}\n\nTo unsubscribe, reply with UNSUBSCRIBE.\n"
    elif record.body_invoice_id is not None and first is not None:
        body = "Dear AP team, our invoice is below.\n\n" + render_body(first.doc)
    else:
        body = _greeting(first, rng)
    if record.original_sender is not None and first is not None:
        inner_date = format_datetime(dt.datetime.fromisoformat(record.date) - dt.timedelta(hours=2))
        name = _name(rng)
        body = (
            f"Hi team, forwarding this for processing.\n\n{name}\n\n"
            "---------- Forwarded message ---------\n"
            f"From: {first.doc.vendor.contacts[0].name} <{record.original_sender}>\n"
            f"Date: {inner_date}\n"
            f"Subject: {_subject([first])}\n"
            f"To: {STAFF}\n\n" + body
        )
    msg.set_content(body)
    for att in record.attachments:
        if att.invoice_id is not None:
            data = blobs[att.invoice_id]
        elif att.filename.endswith(".png"):
            data = _logo_png()
        else:
            data = _small_pdf(att.filename.removesuffix(".pdf").replace("_", " "))
        maintype, subtype = att.content_type.split("/", 1)
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=att.filename)
    for n, part in enumerate(msg.walk()):
        if part.is_multipart():
            part.set_boundary(f"==pairly-{record.id}-{n}==")
    return msg.as_bytes()
