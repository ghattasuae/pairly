# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Vendor onboarding routes (brief 4.1.1; see ``pairly_pay.onboarding``).

The AP team (``pay.vendor.write``):

* ``POST /vendor-invites``               a signed single-use link (7 days). The token is
  returned once, for the web app to show or send; only its hash is stored.
* ``GET  /vendor-invites``               the links and their state
* ``POST /vendor-invites/{id}/revoke``   stop a link (and its open session)

The vendor (no sign-in; rate limited; tokens in the body or a header, never a URL):

* ``POST /public/vendor-onboarding/open``   ``{"token"}``: use the link once, get a session
* ``GET  /vendor-onboarding/me``             the form's context (the organisation's name)
* ``POST /vendor-onboarding/me/documents``   multipart ``file``: a document through the
  engine's intake (sniffed, size and type checked, virus scanned, deduplicated)
* ``POST /vendor-onboarding/me/submit``      the form. Validated like a staff entry
  (IBAN, TRN, phone); becomes a ``pending`` vendor with its bank details ``proposed``
  and the duplicate hints for the AP review. The form's phone number is kept as claimed
  only, never the number AP calls back on (M-05). Ends the session.
"""

from __future__ import annotations

import ipaddress
import math
import os
import tempfile
import time
import uuid
from collections import OrderedDict, deque
from datetime import UTC, datetime
from functools import cache
from pathlib import Path
from typing import Any, Final

from fastapi import APIRouter, BackgroundTasks, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select

from pairly_api.problems import problem_response
from pairly_api.uploads import (
    MULTIPART_OVERHEAD,
    UnsupportedBodyError,
    enqueue_scan,
    get_services,
    receive_upload_with_fields,
    rejected_response,
)
from pairly_core.api_keys.ratelimit import RateLimitedError
from pairly_core.errors import BadRequestError, ConflictError, NotFoundError
from pairly_core.identity import CurrentActor, OrgSession, RequestSession
from pairly_core.ingestion.models import Document, DocumentSource
from pairly_core.ingestion.service import (
    AuditBy,
    RejectReason,
    TooLargeError,
    ingest_spooled,
    record_rejection,
)
from pairly_core.models import Organisation
from pairly_core.ratelimit import window_limiters
from pairly_core.rbac import authorize
from pairly_pay.api.schemas import (
    PayInviteCreatedOut,
    PayInviteIn,
    PayInviteList,
    PayInviteOut,
    PayOnboardingDocumentOut,
    PayOnboardingDoneOut,
    PayOnboardingIn,
    PayOnboardingPageOut,
    PayTokenIn,
    PayVendorSessionOut,
)
from pairly_pay.api.vendors import address_json, contacts_json, person
from pairly_pay.common import INVITE_ENTITY, WORKFLOW, By, audit, clean_text
from pairly_pay.models import PayVendorInvite, PayVendorOnboardingSession, VendorSource
from pairly_pay.onboarding import (
    MAX_DOCUMENTS,
    IssuedInvite,
    VendorIdentity,
    authenticate,
    create_invite,
    invite_email,
    invite_state,
    open_session,
)
from pairly_pay.permissions import VENDOR_WRITE
from pairly_pay.vendors import (
    BankInput,
    VendorInput,
    check_bank,
    check_phone,
    check_trn,
    create_vendor,
    find_duplicates,
)

router = APIRouter(tags=["pay"])

SESSION_HEADER: Final = "X-Pairly-Vendor-Session"
CLIENT_IP_HEADER: Final = "X-Pairly-Client-IP"
_NO_STORE: Final = {"Cache-Control": "no-store"}
_LIMITS: Final = {"open": (30, 3600.0), "session": (300, 3600.0), "files": (40, 3600.0)}


# ------------------------------------------------------------------ rate limits


class _Window:
    """At most ``limit`` hits per key in any ``window`` seconds, in this process. Bounded
    memory. The routes use it as the local half of ``pairly_core.ratelimit``'s shared
    limiter (``_hit``)."""

    def __init__(self, limit: int, window: float, max_keys: int = 50_000) -> None:
        self.limit, self.window, self.max_keys = limit, window, max_keys
        self.hits: OrderedDict[str, deque[float]] = OrderedDict()

    def hit(self, key: str) -> None:
        now = time.monotonic()
        hits = self.hits.get(key)
        if hits is None:
            hits = self.hits[key] = deque()
            if len(self.hits) > self.max_keys:
                self.hits.popitem(last=False)
        else:
            self.hits.move_to_end(key)
        while hits and hits[0] <= now - self.window:
            hits.popleft()
        if len(hits) >= self.limit:
            raise RateLimitedError(max(1, math.ceil(hits[0] + self.window - now)))
        hits.append(now)


@cache
def _trusted_proxies() -> frozenset[str]:
    out: set[str] = set()
    for item in os.environ.get("PAIRLY_TRUSTED_PROXIES", "127.0.0.1,::1").split(","):
        try:
            out.add(str(ipaddress.ip_address(item.strip())))
        except ValueError:
            continue
    return frozenset(out)


def _ip(value: str | None) -> str | None:
    try:
        return str(ipaddress.ip_address((value or "").strip()))
    except ValueError:
        return None


def client_ip(request: Request) -> str:
    peer = _ip(request.client.host if request.client else None)
    if peer is not None and peer in _trusted_proxies():
        forwarded = _ip(request.headers.get(CLIENT_IP_HEADER))
        if forwarded is not None:
            return forwarded
    return peer or "unknown"


async def _hit(request: Request, kind: str, key: str) -> None:
    """Shared limits (Valkey, with this process's window as the fallback; security
    review 2026-10, L-03)."""
    store = window_limiters(
        request, "pay_onboarding_limiters", _LIMITS, "pay.onboarding", local=_Window
    )
    await store[kind].hit(key)


# ------------------------------------------------------------------ the AP team


def _invite_out(invite: PayVendorInvite, now: datetime) -> PayInviteOut:
    return PayInviteOut(
        id=invite.id,
        company_hint=invite.company_hint,
        email=invite_email(invite),
        locale=invite.locale,
        state=invite_state(invite, now),
        expires_at=invite.expires_at,
        used_at=invite.used_at,
        vendor_id=invite.vendor_id,
        created_at=invite.created_at,
    )


@router.post("/vendor-invites", status_code=201, response_model=PayInviteCreatedOut)
async def invite(body: PayInviteIn, actor: CurrentActor, session: OrgSession) -> JSONResponse:
    authorize(actor, VENDOR_WRITE)
    user = person(actor)
    issued: IssuedInvite = await create_invite(
        session,
        actor.org_id,
        created_by=user,
        by=By.of(actor),
        company_hint=clean_text(body.company_hint, 200) or None,
        email=clean_text(body.email, 254).lower() or None,
        locale=body.locale,
    )
    out = PayInviteCreatedOut(
        **_invite_out(issued.invite, datetime.now(UTC)).model_dump(),
        token=issued.token,
        path=f"/{body.locale}/vendor-onboarding#{issued.token}",
    )
    return JSONResponse(out.model_dump(mode="json"), status_code=201, headers=_NO_STORE)


@router.get("/vendor-invites", response_model=PayInviteList)
async def list_invites(actor: CurrentActor, session: OrgSession) -> PayInviteList:
    authorize(actor, VENDOR_WRITE)
    now = datetime.now(UTC)
    rows = await session.scalars(
        select(PayVendorInvite).order_by(PayVendorInvite.created_at.desc()).limit(100)
    )
    return PayInviteList(items=[_invite_out(i, now) for i in rows])


@router.post("/vendor-invites/{invite_id}/revoke", response_model=PayInviteOut)
async def revoke(invite_id: uuid.UUID, actor: CurrentActor, session: OrgSession) -> PayInviteOut:
    authorize(actor, VENDOR_WRITE)
    person(actor)
    row = await session.scalar(
        select(PayVendorInvite).where(PayVendorInvite.id == invite_id).with_for_update()
    )
    if row is None:
        raise NotFoundError("invite not found")
    if row.vendor_id is not None:
        raise ConflictError("the vendor already submitted the form")
    now = datetime.now(UTC)
    if row.revoked_at is None:
        row.revoked_at = now
        await session.flush()
        await audit(
            session, actor, "pay.vendor_invite.revoked", entity_type=INVITE_ENTITY, entity_id=row.id
        )
    return _invite_out(row, now)


# ------------------------------------------------------------------ the vendor


async def _vendor(
    request: Request, session: RequestSession
) -> tuple[VendorIdentity, PayVendorOnboardingSession]:
    identity, row = await authenticate(session, request.headers.get(SESSION_HEADER))
    await _hit(request, "session", str(identity.session_id))
    return identity, row


async def _page(
    session: RequestSession, identity: VendorIdentity, row: PayVendorOnboardingSession
) -> JSONResponse:
    org = await session.scalar(select(Organisation).where(Organisation.id == identity.org_id))
    invite_row = await session.scalar(
        select(PayVendorInvite).where(PayVendorInvite.id == identity.invite_id)
    )
    ids = [uuid.UUID(d) for d in row.document_ids]
    docs = list(await session.scalars(select(Document).where(Document.id.in_(ids)))) if ids else []
    names = {d.id: d.original_filename for d in docs}
    out = PayOnboardingPageOut(
        company=org.name if org else "",
        company_hint=invite_row.company_hint if invite_row else None,
        locale=invite_row.locale if invite_row else "en",
        documents=[PayOnboardingDocumentOut(id=i, filename=names.get(i)) for i in ids],
        max_documents=MAX_DOCUMENTS,
        expires_at=row.expires_at,
    )
    return JSONResponse(out.model_dump(mode="json"), headers=_NO_STORE)


@router.post("/public/vendor-onboarding/open", response_model=PayVendorSessionOut)
async def open_link(body: PayTokenIn, request: Request, session: RequestSession) -> JSONResponse:
    await _hit(request, "open", client_ip(request))
    opened = await open_session(session, body.token)
    out = PayVendorSessionOut(session_token=opened.token, expires_at=opened.expires_at)
    return JSONResponse(out.model_dump(mode="json"), headers=_NO_STORE)


@router.get("/vendor-onboarding/me", response_model=PayOnboardingPageOut)
async def page(request: Request, session: RequestSession) -> JSONResponse:
    identity, row = await _vendor(request, session)
    return await _page(session, identity, row)


@router.post(
    "/vendor-onboarding/me/documents", status_code=201, response_model=PayOnboardingPageOut
)
async def upload(request: Request, session: RequestSession, background: BackgroundTasks) -> Any:
    identity, row = await _vendor(request, session)
    await _hit(request, "files", str(identity.session_id))
    if len(row.document_ids) >= MAX_DOCUMENTS:
        raise ConflictError(f"at most {MAX_DOCUMENTS} documents")
    services = get_services(request)
    policy = services.settings.policy_for(WORKFLOW)
    by = AuditBy(identity.by.actor_type, identity.by.actor_id)
    declared = request.headers.get("content-length")
    if (
        declared is not None
        and declared.isdigit()
        and int(declared) > policy.max_bytes + MULTIPART_OVERHEAD
    ):
        await record_rejection(
            session,
            by=by,
            workflow=WORKFLOW,
            source=DocumentSource.UPLOAD,
            reason=RejectReason.TOO_LARGE,
        )
        return rejected_response(request, RejectReason.TOO_LARGE, WORKFLOW)
    with tempfile.TemporaryDirectory(prefix="pairly-pay-", dir=services.tmp_dir) as tmp:
        try:
            received = await receive_upload_with_fields(
                request, Path(tmp), policy.max_bytes, services.settings.upload_timeout_seconds
            )
        except UnsupportedBodyError:
            return problem_response(
                request,
                415,
                "unsupported_media_type",
                detail="send multipart/form-data with a 'file' part",
            )
        except TooLargeError:
            await record_rejection(
                session,
                by=by,
                workflow=WORKFLOW,
                source=DocumentSource.UPLOAD,
                reason=RejectReason.TOO_LARGE,
            )
            return rejected_response(request, RejectReason.TOO_LARGE, WORKFLOW)
        outcome = await ingest_spooled(
            session,
            services.store,
            spooled=received.file,
            workflow=WORKFLOW,
            source=DocumentSource.UPLOAD,
            filename=received.filename,
            policy=policy,
            by=by,
        )
    if outcome.reason is not None or outcome.document is None:
        return rejected_response(request, outcome.reason or RejectReason.EMPTY, WORKFLOW)
    document = outcome.document
    if str(document.id) not in row.document_ids:
        row.document_ids = [*row.document_ids, str(document.id)]
    await session.flush()
    await audit(
        session,
        identity.by,
        "pay.vendor_invite.document_added",
        entity_type=INVITE_ENTITY,
        entity_id=identity.invite_id,
        payload={"document_id": str(document.id), "seen_before": not outcome.created},
    )
    response = await _page(session, identity, row)
    await session.commit()
    if outcome.created:
        background.add_task(enqueue_scan, services.queue, identity.org_id, document.id)
    response.status_code = 201
    return response


@router.post("/vendor-onboarding/me/submit", status_code=201, response_model=PayOnboardingDoneOut)
async def submit(body: PayOnboardingIn, request: Request, session: RequestSession) -> JSONResponse:
    identity, row = await _vendor(request, session)
    invite_row = await session.scalar(
        select(PayVendorInvite).where(PayVendorInvite.id == identity.invite_id).with_for_update()
    )
    if invite_row is None or invite_row.vendor_id is not None:
        raise ConflictError("this form was already submitted")
    trn = check_trn(body.trn)
    bank = check_bank(BankInput(**body.bank.model_dump()))
    phone = check_phone(body.phone_on_file)
    if phone is None:
        raise BadRequestError("phone_format")
    hints = await find_duplicates(
        session,
        identity.org_id,
        legal_name=body.legal_name,
        trade_name=body.trade_name,
        trn=trn,
        iban=bank.iban,
    )
    reasons = [f"possible_duplicate:{h.reference}:{'+'.join(h.reasons)}" for h in hints]
    reasons += [f"bank:{w}" for w in bank.warnings]
    reasons.append("trn_not_verified" if trn else "no_trn")
    vendor = await create_vendor(
        session,
        identity.org_id,
        VendorInput(
            legal_name=clean_text(body.legal_name, 200),
            country=body.country,
            legal_name_ar=clean_text(body.legal_name_ar, 200) or None,
            trade_name=clean_text(body.trade_name, 200) or None,
            trade_licence=clean_text(body.trade_licence, 64) or None,
            trn=trn,
            address=address_json(body.address),
            contacts=contacts_json(body.contacts),
            # Never the number on file: AP records that one from an independent source
            # when it accepts the onboarding (security review 2026-10, M-05).
            phone_claimed=phone,
            email_domains=sorted(set(body.email_domains)),
            category=clean_text(body.category, 64) or None,
        ),
        by=identity.by,
        bank=bank,
        source=VendorSource.ONBOARDING,
        review_reasons=reasons,
        document_ids=list(row.document_ids),
    )
    now = datetime.now(UTC)
    invite_row.vendor_id = vendor.id
    row.submitted_at = now
    await session.flush()
    await audit(
        session,
        identity.by,
        "pay.vendor_invite.submitted",
        entity_type=INVITE_ENTITY,
        entity_id=identity.invite_id,
        payload={
            "vendor_id": str(vendor.id),
            "documents": len(row.document_ids),
            "duplicate_hints": len(hints),
        },
    )
    out = PayOnboardingDoneOut(reference=vendor.reference, status="received")
    return JSONResponse(out.model_dump(mode="json"), status_code=201, headers=_NO_STORE)
