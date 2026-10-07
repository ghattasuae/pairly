# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Inbound email: address scheme, RFC 822 parsing and ``ingest_email``.

Addresses: every organisation gets one address per workflow,

    <workflow>+<org-slug>@<PAIRLY_INBOUND_EMAIL_DOMAIN>     for example pay+acme@inbound.example.com

The recipient is taken from the SMTP envelope when the transport provides it, then from
``Delivered-To``, ``X-Original-To``, ``To`` and ``Cc``. The first address on our domain
that names a registered workflow and an active organisation wins. The organisation is
found through ``inbound_org_id(slug)``, a narrow SECURITY DEFINER function (migration
0004) that returns an id for an active organisation slug and nothing else.

Parsing (``parse_email``) keeps what intake needs: sender, Message-ID, subject, date,
the text body (HTML converted to text), attachments (including those inside forwarded
``message/rfc822`` parts, to a fixed depth), the forwarded chain (attached and inline
"Forwarded message" blocks) and bulk-mail hints. Inline images referenced by the HTML
(``Content-ID``, signature logos) are not treated as attachments.

``ingest_email`` stores each attachment through the normal ingestion path (sniffing,
policy, dedupe, storage, audit). When no attachment was accepted and the body has text,
the body is stored as a ``text/plain`` document (an invoice written in the body), if the
workflow's policy allows ``text/plain``.

Transport is not part of this module: an ``InboundEmailSource`` (Mailpit, an SMTP
receiver, a provider webhook) hands raw bytes to ``ingest_email``.
TODO(WP1.9): a Mailpit or SMTP based ``InboundEmailSource`` and receipt replies.
"""

from __future__ import annotations

import hashlib
import re
import tempfile
import uuid
from collections.abc import AsyncIterator, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from email import policy
from email.headerregistry import Address
from email.message import EmailMessage, Message
from email.parser import BytesParser
from email.utils import getaddresses, parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Final, Protocol, runtime_checkable

import structlog
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from pairly_core.audit import append_audit
from pairly_core.db import set_tenant
from pairly_core.ingestion.models import DocumentSource, InboundEmail
from pairly_core.ingestion.service import (
    AuditBy,
    DocumentAction,
    IngestOutcome,
    RejectReason,
    ingest_spooled,
    record_rejection,
    spool_bytes,
)
from pairly_core.ingestion.settings import IngestionSettings, IntakePolicy
from pairly_core.ingestion.sniff import TEXT
from pairly_core.ingestion.storage import ObjectStore

log = structlog.get_logger(__name__)

EMAIL_ACTOR: Final = "pairly.ingest.email"
MAX_FORWARD_DEPTH: Final = 3
MAX_PART_DEPTH: Final = 25
MAX_SUBJECT: Final = 998
MIN_BODY_CHARS: Final = 20

_WORKFLOW_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")


# ------------------------------------------------------------------ addresses


@dataclass(frozen=True, slots=True)
class InboundRoute:
    workflow: str
    org_slug: str


def inbound_address(workflow: str, org_slug: str, domain: str) -> str:
    """The inbound address of one organisation and workflow."""
    if not _WORKFLOW_RE.fullmatch(workflow) or not _SLUG_RE.fullmatch(org_slug):
        raise ValueError("invalid workflow or organisation slug")
    return f"{workflow}+{org_slug}@{domain.lower()}"


def parse_inbound_address(address: str, domain: str) -> InboundRoute | None:
    """``pay+acme@<domain>`` -> ``InboundRoute("pay", "acme")``; anything else -> None."""
    _, addr = getaddresses([address])[0] if address else ("", "")
    local, at, host = addr.strip().lower().rpartition("@")
    if not at or host != domain.lower():
        return None
    workflow, plus, slug = local.partition("+")
    if not plus or not _WORKFLOW_RE.fullmatch(workflow) or not _SLUG_RE.fullmatch(slug):
        return None
    return InboundRoute(workflow, slug)


def resolve_route(
    recipients: Iterable[str], domain: str, workflows: frozenset[str]
) -> InboundRoute | None:
    for recipient in recipients:
        route = parse_inbound_address(recipient, domain)
        if route is not None and route.workflow in workflows:
            return route
    return None


# ------------------------------------------------------------------ parsed model


@dataclass(frozen=True, slots=True)
class EmailAttachment:
    filename: str | None
    # Declared by the sender, informational only: intake sniffs the bytes.
    declared_type: str
    data: bytes
    # Depth 0: attached to the received email; 1+: inside a forwarded message.
    depth: int = 0


@dataclass(frozen=True, slots=True)
class ForwardedMessage:
    sender: str | None
    subject: str | None
    date: str | None
    message_id: str | None = None
    # "attached" (message/rfc822 part) or "inline" (quoted in the body).
    kind: str = "attached"

    def to_json(self) -> dict[str, Any]:
        return {
            "sender": self.sender,
            "subject": self.subject,
            "date": self.date,
            "message_id": self.message_id,
            "kind": self.kind,
        }


@dataclass
class ParsedEmail:
    message_id: str
    raw_sha256: str
    sender: str | None
    sender_name: str | None
    reply_to: str | None
    recipients: list[str]
    subject: str | None
    sent_at: datetime | None
    text_body: str
    html_body: str | None
    attachments: list[EmailAttachment] = field(default_factory=list)
    forwarded: list[ForwardedMessage] = field(default_factory=list)
    is_bulk: bool = False
    in_reply_to: str | None = None
    skipped_inline_images: int = 0
    malformed: bool = False

    @property
    def sender_domain(self) -> str | None:
        if not self.sender or "@" not in self.sender:
            return None
        return self.sender.rsplit("@", 1)[1].lower()


class _HTMLText(HTMLParser):
    _BLOCK = frozenset(
        {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6", "table", "section"}
    )
    _SKIP = frozenset({"script", "style", "head", "title"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._SKIP:
            self._skip += 1
        elif tag in self._BLOCK:
            self.parts.append("\n")
        elif tag in {"td", "th"}:
            self.parts.append("\t")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP and self._skip:
            self._skip -= 1
        elif tag in self._BLOCK:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    parser = _HTMLText()
    try:
        parser.feed(html)
        parser.close()
    except (ValueError, AssertionError):  # pragma: no cover - HTMLParser is lenient
        return ""
    raw_lines = "".join(parser.parts).split("\n")
    lines = [re.sub(r"[ \t\u00a0]+", " ", line).strip() for line in raw_lines]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


# ------------------------------------------------------------------ parsing

_FORWARD_MARKERS = re.compile(
    r"^\s*(?:>\s*)*(?:-{2,}\s*Forwarded message\s*-{2,}|Begin forwarded message:|"
    r"-{2,}\s*Original Message\s*-{2,}|_{10,})\s*$",
    re.IGNORECASE | re.MULTILINE,
)
_QUOTED_HEADER = re.compile(
    r"^\s*(?:>\s*)*(From|Date|Sent|Subject|To|Cc)\s*:\s*(.*)$", re.IGNORECASE
)


def _header(msg: Message, name: str) -> str | None:
    try:
        value = msg.get(name)
    except Exception:  # malformed header: treat as absent
        return None
    if value is None:
        return None
    return " ".join(str(value).split())[:MAX_SUBJECT] or None


def _addresses(msg: Message, *names: str) -> list[str]:
    found: list[str] = []
    for name in names:
        try:
            values = msg.get_all(name) or []
        except Exception:  # noqa: S112  # a malformed header is treated as absent
            continue
        for _, addr in getaddresses([str(v) for v in values]):
            if addr and "@" in addr:
                found.append(addr.strip().lower())
    return found


def _sender(msg: Message) -> tuple[str | None, str | None]:
    try:
        header = msg["From"]
        addresses: Sequence[Address] = getattr(header, "addresses", ()) or ()
    except Exception:
        addresses = ()
    if addresses:
        first = addresses[0]
        spec = first.addr_spec.strip().lower() if first.addr_spec else None
        return (spec if spec and "@" in spec else None), (first.display_name or None)
    fallback = _addresses(msg, "From")
    return (fallback[0] if fallback else None), None


def _date(msg: Message) -> datetime | None:
    raw = _header(msg, "Date")
    if not raw:
        return None
    try:
        value = parsedate_to_datetime(raw)
    except (TypeError, ValueError, IndexError):
        return None
    return value if value.tzinfo is not None else None


def _message_id(msg: Message) -> str | None:
    raw = _header(msg, "Message-ID")
    if not raw:
        return None
    return raw.strip().strip("<>").strip()[:900] or None


def _is_bulk(msg: Message) -> bool:
    precedence = (_header(msg, "Precedence") or "").lower()
    auto = (_header(msg, "Auto-Submitted") or "no").lower()
    return bool(
        _header(msg, "List-Unsubscribe")
        or _header(msg, "List-Id")
        or precedence in {"bulk", "list", "junk"}
        or auto != "no"
    )


def _decode_text(part: Message) -> str:
    try:
        content = part.get_content() if isinstance(part, EmailMessage) else None
    except (LookupError, ValueError, AssertionError):
        content = None
    if isinstance(content, str):
        return content
    payload = part.get_payload(decode=True)
    if isinstance(payload, bytes):
        charset = part.get_content_charset() or "utf-8"
        try:
            return payload.decode(charset, errors="replace")
        except LookupError:
            return payload.decode("utf-8", errors="replace")
    return ""


def _inline_forwards(body: str) -> list[ForwardedMessage]:
    found: list[ForwardedMessage] = []
    for marker in _FORWARD_MARKERS.finditer(body):
        headers: dict[str, str] = {}
        for line in body[marker.end() :].lstrip("\n").splitlines()[:12]:
            match = _QUOTED_HEADER.match(line)
            if match:
                headers.setdefault(match.group(1).lower(), match.group(2).strip()[:MAX_SUBJECT])
            elif headers and not line.strip(" >"):
                break
        if "from" not in headers and "subject" not in headers:
            continue
        sender_raw = headers.get("from")
        sender = None
        if sender_raw:
            addr = getaddresses([sender_raw])[0][1]
            sender = addr.lower() if "@" in addr else sender_raw
        found.append(
            ForwardedMessage(
                sender=sender,
                subject=headers.get("subject"),
                date=headers.get("date") or headers.get("sent"),
                kind="inline",
            )
        )
    return found


def parse_email(raw: bytes) -> ParsedEmail:
    """Parse one raw RFC 822 message. Never raises on malformed input (``malformed`` flag)."""
    raw_sha = hashlib.sha256(raw).hexdigest()
    malformed = False
    try:
        msg = BytesParser(policy=policy.default).parsebytes(raw)
    except (RecursionError, ValueError, TypeError):
        msg = EmailMessage()
        malformed = True
    if getattr(msg, "defects", None):
        malformed = True

    sender, sender_name = _sender(msg)
    reply_to = next(iter(_addresses(msg, "Reply-To")), None)
    message_id = _message_id(msg) or f"{raw_sha}@pairly.invalid"
    parsed = ParsedEmail(
        message_id=message_id,
        raw_sha256=raw_sha,
        sender=sender,
        sender_name=sender_name,
        reply_to=reply_to,
        recipients=_addresses(msg, "Delivered-To", "X-Original-To", "To", "Cc"),
        subject=_header(msg, "Subject"),
        sent_at=_date(msg),
        text_body="",
        html_body=None,
        is_bulk=_is_bulk(msg),
        in_reply_to=_header(msg, "In-Reply-To"),
        malformed=malformed,
    )

    body_parts: set[int] = set()
    if isinstance(msg, EmailMessage):
        try:
            plain = msg.get_body(preferencelist=("plain",))
            html = msg.get_body(preferencelist=("html",))
        except (KeyError, ValueError, RecursionError):
            plain = html = None
            parsed.malformed = True
        if html is not None:
            body_parts.add(id(html))
            parsed.html_body = _decode_text(html)
        if plain is not None:
            body_parts.add(id(plain))
            parsed.text_body = _decode_text(plain).strip()
        elif parsed.html_body:
            parsed.text_body = html_to_text(parsed.html_body)

    try:
        _walk(msg, parsed, body_parts, forward_depth=0, part_depth=0)
    except RecursionError:
        parsed.malformed = True
    parsed.forwarded.extend(_inline_forwards(parsed.text_body))
    return parsed


def _walk(
    part: Message,
    parsed: ParsedEmail,
    body_parts: set[int],
    *,
    forward_depth: int,
    part_depth: int,
) -> None:
    if part_depth > MAX_PART_DEPTH:
        parsed.malformed = True
        return
    ctype = part.get_content_type()
    if ctype == "message/rfc822":
        payload = part.get_payload()
        inner = payload[0] if isinstance(payload, list) and payload else None
        if not isinstance(inner, Message):
            return
        sender, _ = _sender(inner)
        parsed.forwarded.append(
            ForwardedMessage(
                sender=sender,
                subject=_header(inner, "Subject"),
                date=_header(inner, "Date"),
                message_id=_message_id(inner),
            )
        )
        if forward_depth < MAX_FORWARD_DEPTH:
            inner_bodies: set[int] = set()
            if isinstance(inner, EmailMessage):
                for pref in (("plain",), ("html",)):
                    try:
                        found = inner.get_body(preferencelist=pref)
                    except (KeyError, ValueError):
                        found = None
                    if found is not None:
                        inner_bodies.add(id(found))
            _walk(
                inner,
                parsed,
                inner_bodies,
                forward_depth=forward_depth + 1,
                part_depth=part_depth + 1,
            )
        return
    if part.is_multipart():
        payload = part.get_payload()
        for sub in payload if isinstance(payload, list) else []:
            if isinstance(sub, Message):
                _walk(
                    sub,
                    parsed,
                    body_parts,
                    forward_depth=forward_depth,
                    part_depth=part_depth + 1,
                )
        return
    if id(part) in body_parts:
        return
    disposition = part.get_content_disposition()
    try:
        filename = part.get_filename()
    except Exception:
        filename = None
    if ctype.startswith("text/") and disposition != "attachment" and not filename:
        return  # an alternative rendering of the body, not a file
    if disposition == "inline" and part.get("Content-ID") and ctype.startswith("image/"):
        parsed.skipped_inline_images += 1
        return
    data = part.get_payload(decode=True)
    if not isinstance(data, bytes):
        return
    parsed.attachments.append(
        EmailAttachment(filename=filename, declared_type=ctype, data=data, depth=forward_depth)
    )


# ------------------------------------------------------------------ sources


@dataclass(frozen=True, slots=True)
class RawInboundEmail:
    raw: bytes
    # SMTP envelope recipients (RCPT TO), when the transport knows them.
    envelope_recipients: tuple[str, ...] = ()
    # Transport reference for acknowledgement (queue id, file name, API id).
    ref: str | None = None


@runtime_checkable
class InboundEmailSource(Protocol):
    """Where raw inbound email comes from (an SMTP receiver, Mailpit, a provider API)."""

    def messages(self) -> AsyncIterator[RawInboundEmail]: ...

    async def acknowledge(self, message: RawInboundEmail, *, ok: bool) -> None:
        """Called after ``ingest_email``; ``ok`` False keeps the message for a retry."""
        ...


# ------------------------------------------------------------------ ingest


@dataclass(frozen=True, slots=True)
class EmailItem:
    filename: str | None
    outcome: str
    document_id: uuid.UUID | None = None
    reason: str | None = None


@dataclass
class EmailIngestResult:
    # ingested | duplicate | unroutable | too_large
    status: str
    org_id: uuid.UUID | None = None
    workflow: str | None = None
    inbound_email_id: uuid.UUID | None = None
    message_id: str | None = None
    items: list[EmailItem] = field(default_factory=list)
    # Documents created by this email (to queue for scanning after commit).
    created: list[uuid.UUID] = field(default_factory=list)


async def lookup_inbound_org(
    sessionmaker: async_sessionmaker[AsyncSession], slug: str
) -> uuid.UUID | None:
    async with sessionmaker() as session, session.begin():
        value = await session.scalar(text("SELECT inbound_org_id(:slug)"), {"slug": slug})
    return value if isinstance(value, uuid.UUID) else None


async def ingest_email(
    raw: bytes,
    *,
    sessionmaker: async_sessionmaker[AsyncSession],
    store: ObjectStore,
    settings: IngestionSettings,
    workflows: frozenset[str],
    envelope_recipients: Sequence[str] = (),
    workdir: Path | None = None,
) -> EmailIngestResult:
    """Route, parse and ingest one raw email. Commits its own transaction.

    Unroutable or oversized messages write nothing (there is no organisation to audit
    them in) and are logged; the caller decides whether to bounce them.
    """
    if len(raw) > settings.max_email_bytes:
        log.warning("email_too_large", size=len(raw))
        return EmailIngestResult("too_large")
    parsed = parse_email(raw)
    route = resolve_route(
        [*envelope_recipients, *parsed.recipients], settings.inbound_email_domain, workflows
    )
    org_id = await lookup_inbound_org(sessionmaker, route.org_slug) if route else None
    if route is None or org_id is None:
        log.info("email_unroutable", sender_domain=parsed.sender_domain)
        return EmailIngestResult("unroutable", message_id=parsed.message_id)

    result = EmailIngestResult(
        "ingested", org_id=org_id, workflow=route.workflow, message_id=parsed.message_id
    )
    by = AuditBy.system(EMAIL_ACTOR)
    policy_ = settings.policy_for(route.workflow)
    async with sessionmaker() as session:
        set_tenant(session, org_id, None)
        async with session.begin():
            existing = await session.scalar(
                select(InboundEmail).where(InboundEmail.message_id == parsed.message_id)
            )
            if existing is not None:
                result.status = "duplicate"
                result.inbound_email_id = existing.id
                await append_audit(
                    session,
                    action=DocumentAction.EMAIL_DUPLICATE,
                    actor_type=by.actor_type,
                    actor_id=by.actor_id,
                    entity_type="inbound_email",
                    entity_id=existing.id,
                    workflow=route.workflow,
                    payload={"raw_sha256": parsed.raw_sha256},
                )
                return result

            email_row = InboundEmail(
                workflow=route.workflow,
                message_id=parsed.message_id,
                raw_sha256=parsed.raw_sha256,
                sender_address=parsed.sender,
                sender_domain=parsed.sender_domain,
                subject=parsed.subject,
                sent_at=parsed.sent_at,
                forwarded_chain=[f.to_json() for f in parsed.forwarded],
                is_bulk=parsed.is_bulk,
                attachment_count=len(parsed.attachments),
            )
            session.add(email_row)
            await session.flush()
            result.inbound_email_id = email_row.id

            with tempfile.TemporaryDirectory(prefix="pairly-email-", dir=workdir) as tmp:
                await _ingest_parts(
                    session,
                    store,
                    parsed,
                    result,
                    Path(tmp),
                    route.workflow,
                    policy_,
                    by,
                    settings.max_email_attachments,
                )

            counts = {
                kind: sum(1 for item in result.items if item.outcome == kind)
                for kind in ("created", "duplicate", "rejected")
            }
            await append_audit(
                session,
                action=DocumentAction.EMAIL_RECEIVED,
                actor_type=by.actor_type,
                actor_id=by.actor_id,
                entity_type="inbound_email",
                entity_id=email_row.id,
                workflow=route.workflow,
                payload={
                    "raw_sha256": parsed.raw_sha256,
                    "sender_domain": parsed.sender_domain,
                    "attachments": len(parsed.attachments),
                    "forwarded": len(parsed.forwarded),
                    "is_bulk": parsed.is_bulk,
                    "malformed": parsed.malformed,
                    **counts,
                },
            )
    log.info(
        "email_ingested",
        inbound_email_id=str(result.inbound_email_id),
        workflow=route.workflow,
        documents=len(result.created),
    )
    return result


async def _ingest_parts(
    session: AsyncSession,
    store: ObjectStore,
    parsed: ParsedEmail,
    result: EmailIngestResult,
    tmp: Path,
    workflow: str,
    policy_: IntakePolicy,
    by: AuditBy,
    max_attachments: int,
) -> None:
    for index, attachment in enumerate(parsed.attachments):
        if index >= max_attachments:
            await record_rejection(
                session,
                by=by,
                workflow=workflow,
                source=DocumentSource.EMAIL,
                reason=RejectReason.TOO_MANY_ATTACHMENTS,
                size=len(attachment.data),
            )
            result.items.append(
                EmailItem(attachment.filename, "rejected", reason="too_many_attachments")
            )
            continue
        outcome = await _ingest_one(
            session,
            store,
            tmp,
            attachment.data,
            attachment.filename,
            workflow,
            policy_,
            by,
            result.inbound_email_id,
        )
        _record_item(result, attachment.filename, outcome)

    body = parsed.text_body.strip()
    accepted = any(item.outcome in {"created", "duplicate"} for item in result.items)
    if not accepted and len(body) >= MIN_BODY_CHARS and TEXT in policy_.allowed_types:
        outcome = await _ingest_one(
            session,
            store,
            tmp,
            body.encode("utf-8"),
            "email-body.txt",
            workflow,
            policy_,
            by,
            result.inbound_email_id,
            media_type=TEXT,
        )
        _record_item(result, "email-body.txt", outcome)


async def _ingest_one(
    session: AsyncSession,
    store: ObjectStore,
    tmp: Path,
    data: bytes,
    filename: str | None,
    workflow: str,
    policy_: IntakePolicy,
    by: AuditBy,
    inbound_email_id: uuid.UUID | None,
    *,
    media_type: str | None = None,
) -> IngestOutcome:
    if len(data) > policy_.max_bytes:
        await record_rejection(
            session,
            by=by,
            workflow=workflow,
            source=DocumentSource.EMAIL,
            reason=RejectReason.TOO_LARGE,
            size=len(data),
        )
        return IngestOutcome("rejected", reason=RejectReason.TOO_LARGE)
    spooled = await spool_bytes(data, tmp, max_bytes=policy_.max_bytes)
    try:
        return await ingest_spooled(
            session,
            store,
            spooled=spooled,
            workflow=workflow,
            source=DocumentSource.EMAIL,
            filename=filename,
            policy=policy_,
            by=by,
            inbound_email_id=inbound_email_id,
            media_type=media_type,
        )
    finally:
        spooled.path.unlink(missing_ok=True)


def _record_item(result: EmailIngestResult, filename: str | None, outcome: IngestOutcome) -> None:
    doc_id = outcome.document.id if outcome.document is not None else None
    reason = outcome.reason.value if outcome.reason is not None else None
    result.items.append(EmailItem(filename, outcome.kind, doc_id, reason))
    if outcome.created and doc_id is not None:
        result.created.append(doc_id)
