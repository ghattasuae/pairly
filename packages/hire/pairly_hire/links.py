# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Signed Hire links: candidate communication preferences and employee referrals.

Both are HMAC-SHA256 tokens under ``PAIRLY_SIGNED_LINK_SECRET`` (the notification
settings' ``signed_link_secret``), with a purpose label in the MAC so a token for one
purpose never works for the other, compared in constant time:

* ``hp1.<org>.<candidate>.<issued>.<mac>``: opens one candidate's preferences in one
  organisation. It expires after ``PREFS_MAX_AGE_SECONDS`` (180 days: it travels in
  emails a candidate may read late). Not single use: changing a preference twice is
  harmless.
* ``rf1.<org>.<link>.<mac>``: one employee's referral link for one job
  (``hire_referral_links`` row). It does not expire; revoking the row ends it.

Ids are hex UUIDs, so a token is URL safe as is. A garbled, forged or expired token
reads as ``None``; the reason is never shown to the caller.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import time
import uuid
from dataclasses import dataclass
from typing import Final

from pairly_core.notifications.settings import NotificationSettings, load_notification_settings

PREFS_PREFIX: Final = "hp1"
REFERRAL_PREFIX: Final = "rf1"
PREFS_MAX_AGE_SECONDS: Final = 180 * 24 * 3600
_SKEW_SECONDS: Final = 300
MAX_TOKEN: Final = 200


@dataclass(frozen=True, slots=True)
class LinkConfig:
    secret: str | None
    web_url: str | None

    @property
    def ready(self) -> bool:
        return bool(self.secret) and bool(self.web_url)


def pipeline_path(
    job_id: uuid.UUID, *, candidate: uuid.UUID | None = None, sla: bool = False
) -> str:
    """The app path of a job's pipeline (a notification's in-app link, no locale)."""
    if candidate is not None:
        return f"/hire/jobs/{job_id}/pipeline?candidate={candidate}"
    return f"/hire/jobs/{job_id}/pipeline?sla=1" if sla else f"/hire/jobs/{job_id}/pipeline"


def kit_path(application_id: uuid.UUID) -> str:
    """The app path of an application's interview kit, where its scorecards are."""
    return f"/hire/applications/{application_id}/kit"


def links_from_settings(settings: NotificationSettings | None) -> LinkConfig:
    if settings is None:
        return LinkConfig(None, None)
    secret = settings.signed_link_secret.get_secret_value() if settings.signed_link_secret else None
    web = (settings.public_web_url or "").rstrip("/") or None
    return LinkConfig(secret, web)


def links_from_env() -> LinkConfig:
    """For the worker: the same settings, read from the environment. Never raises."""
    try:
        return links_from_settings(load_notification_settings())
    except Exception:
        web = os.environ.get("PAIRLY_PUBLIC_WEB_URL", "").rstrip("/") or None
        return LinkConfig(None, web)


def _mac(secret: str, purpose: str, body: str) -> str:
    digest = hmac.new(
        secret.encode("utf-8"), f"pairly.hire.{purpose}|{body}".encode(), hashlib.sha256
    ).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def _uuid(value: str) -> uuid.UUID | None:
    if len(value) != 32:
        return None
    try:
        return uuid.UUID(hex=value)
    except ValueError:
        return None


# ------------------------------------------------------------------ preferences


@dataclass(frozen=True, slots=True)
class PrefsClaim:
    org_id: uuid.UUID
    candidate_id: uuid.UUID


def prefs_token(
    secret: str, org_id: uuid.UUID, candidate_id: uuid.UUID, *, now: float | None = None
) -> str:
    issued = int(time.time() if now is None else now)
    body = f"{PREFS_PREFIX}.{org_id.hex}.{candidate_id.hex}.{issued}"
    return f"{body}.{_mac(secret, 'prefs', body)}"


def read_prefs_token(secret: str, token: str, *, now: float | None = None) -> PrefsClaim | None:
    if not isinstance(token, str) or len(token) > MAX_TOKEN:
        return None
    parts = token.split(".")
    if len(parts) != 5 or parts[0] != PREFS_PREFIX:
        return None
    body = ".".join(parts[:4])
    if not hmac.compare_digest(_mac(secret, "prefs", body), parts[4]):
        return None
    org_id, candidate_id = _uuid(parts[1]), _uuid(parts[2])
    issued = parts[3]
    if org_id is None or candidate_id is None or not issued.isascii() or not issued.isdigit():
        return None
    if len(issued) > 12:
        return None
    age = (time.time() if now is None else now) - int(issued)
    if age < -_SKEW_SECONDS or age > PREFS_MAX_AGE_SECONDS:
        return None
    return PrefsClaim(org_id, candidate_id)


def prefs_url(config: LinkConfig, org_id: uuid.UUID, candidate_id: uuid.UUID, locale: str) -> str:
    """The candidate's preferences page. The token rides in the fragment, so it never
    reaches a server log; the page posts it in a JSON body."""
    if not config.ready:
        raise ValueError("links are not configured")
    assert config.secret is not None  # noqa: S101 (ready)
    token = prefs_token(config.secret, org_id, candidate_id)
    return f"{config.web_url}/{locale}/candidate/preferences#{token}"


# ------------------------------------------------------------------ referrals


@dataclass(frozen=True, slots=True)
class ReferralClaim:
    org_id: uuid.UUID
    link_id: uuid.UUID


def referral_token(secret: str, org_id: uuid.UUID, link_id: uuid.UUID) -> str:
    body = f"{REFERRAL_PREFIX}.{org_id.hex}.{link_id.hex}"
    return f"{body}.{_mac(secret, 'referral', body)}"


def read_referral_token(secret: str, token: str) -> ReferralClaim | None:
    if not isinstance(token, str) or len(token) > MAX_TOKEN:
        return None
    parts = token.split(".")
    if len(parts) != 4 or parts[0] != REFERRAL_PREFIX:
        return None
    body = ".".join(parts[:3])
    if not hmac.compare_digest(_mac(secret, "referral", body), parts[3]):
        return None
    org_id, link_id = _uuid(parts[1]), _uuid(parts[2])
    if org_id is None or link_id is None:
        return None
    return ReferralClaim(org_id, link_id)
