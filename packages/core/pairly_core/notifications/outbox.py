# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Development outbox: email written to files instead of sent (native stack, no Mailpit).

Selected with ``PAIRLY_EMAIL_TRANSPORT=outbox`` and ``PAIRLY_OUTBOX_DIR``; the settings
refuse it outside development and test. The web app writes the same format
(``apps/web/src/lib/outbox.ts``) into the same folder, and the e2e suite reads it
(``apps/web/e2e/utils/mail.ts``).

Format, version 1. For every message, in ``PAIRLY_OUTBOX_DIR``:

* ``<id>.eml``: the RFC 5322 message, exactly as it would have been sent.
* ``<id>.json``: ``{"v", "id", "created", "source", "from", "to", "subject", "text",
  "html", "eml"}`` (``to`` is a list of bare addresses), plus ``attachments``
  (``[{"filename", "content_type", "text"}]``) when the message has any.
* one line appended to ``index.jsonl``: the same object without ``text`` and ``html``.

``id`` is ``<epoch milliseconds, 13 digits>-<16 hex>``, so names sort by time. Files are
written to a temp name and renamed, and the index line is appended last (one ``write``
with ``O_APPEND``), so a reader that follows the index never sees half a message.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import time
from datetime import UTC, datetime
from email.message import EmailMessage
from email.policy import SMTP
from email.utils import getaddresses
from pathlib import Path
from typing import Any, Final

from pairly_core.notifications.email import DeliveryError, OutgoingEmail, build_message

FORMAT_VERSION: Final = 1
INDEX_FILE: Final = "index.jsonl"


def new_message_id() -> str:
    return f"{int(time.time() * 1000):013d}-{secrets.token_hex(8)}"


def _write_atomic(path: Path, data: bytes) -> None:
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
    try:
        with tmp.open("xb") as out:
            out.write(data)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def write_outbox_message(
    directory: Path,
    *,
    raw: bytes,
    sender: str,
    to: list[str],
    subject: str,
    text: str,
    html: str | None,
    source: str,
    attachments: list[dict[str, str]] | None = None,
) -> str:
    """Write one message in outbox format version 1. Returns its id."""
    directory.mkdir(parents=True, exist_ok=True)
    message_id = new_message_id()
    entry: dict[str, Any] = {
        "v": FORMAT_VERSION,
        "id": message_id,
        "created": datetime.now(UTC).isoformat(),
        "source": source,
        "from": sender,
        "to": to,
        "subject": subject,
        "eml": f"{message_id}.eml",
    }
    _write_atomic(directory / f"{message_id}.eml", raw)
    full = entry | {"text": text, "html": html}
    if attachments:
        full["attachments"] = attachments
    _write_atomic(directory / f"{message_id}.json", json.dumps(full).encode("utf-8"))
    line = (json.dumps(entry) + "\n").encode("utf-8")
    fd = os.open(directory / INDEX_FILE, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, line)
    finally:
        os.close(fd)
    return message_id


class OutboxTransport:
    """``EmailTransport`` that writes each message to the development outbox."""

    name = "outbox"

    def __init__(self, directory: Path, *, source: str = "python") -> None:
        self.directory = Path(directory)
        self.source = source

    def _write(self, email: OutgoingEmail, msg: EmailMessage) -> str:
        to = [addr for _, addr in getaddresses([str(msg["To"])]) if addr]
        return write_outbox_message(
            self.directory,
            raw=msg.as_bytes(policy=SMTP),
            sender=str(msg["From"]),
            to=to,
            subject=str(msg["Subject"]),
            text=email.text,
            html=email.html,
            source=self.source,
            attachments=[
                {
                    "filename": a.filename,
                    "content_type": "; ".join(
                        [a.mime_type, *(f"{k}={v}" for k, v in sorted(a.params.items()))]
                    ),
                    "text": a.text,
                }
                for a in email.attachments
            ]
            or None,
        )

    async def send(self, email: OutgoingEmail) -> str:
        msg = build_message(email)
        try:
            await asyncio.to_thread(self._write, email, msg)
        except OSError:
            raise DeliveryError("outbox_unavailable", transient=True) from None
        return str(msg["Message-ID"])
