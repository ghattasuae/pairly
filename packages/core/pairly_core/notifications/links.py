# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Where notifications point: app paths, and full links for messages that leave the app.

A notification's in-app link (``Extras.link``) is a same-origin relative path of the web
app without a locale (``/inbox?item=<id>``): the bell puts the viewer's locale in front
and refuses anything else (a full URL, another host, ``//host``). ``notify`` refuses a
link that is not such a path, so a broken link never reaches the feed.

Email and Slack text need a full link: ``absolute_url`` joins the public web address and
the same path (the web app redirects a path without a locale to the viewer's locale).

Every path built here exists in the web app; a test maps each one to its page.
"""

from __future__ import annotations

import os
import re
import uuid
from typing import Final

# A path of the app: a single leading slash, plain segments, an optional simple query.
_APP_PATH: Final = re.compile(r"^(?:/[A-Za-z0-9_-]{1,128})+/?(?:\?[A-Za-z0-9_=&%.:-]{0,512})?$")
MAX_PATH: Final = 1024


def is_app_path(link: str) -> bool:
    """A same-origin relative path the bell can follow (``/pay/bank-changes/<id>``)."""
    return len(link) <= MAX_PATH and _APP_PATH.fullmatch(link) is not None


def app_path(link: str) -> str:
    if not is_app_path(link):
        raise ValueError("not an app path")
    return link


def public_web_url() -> str | None:
    """The public web address (``PAIRLY_PUBLIC_WEB_URL``), or None when not configured."""
    value = os.environ.get("PAIRLY_PUBLIC_WEB_URL", "").strip().rstrip("/")
    return value if value.startswith(("https://", "http://")) else None


def absolute_url(web_url: str | None, path: str) -> str | None:
    """The full link to ``path`` on ``web_url`` (None without a web address)."""
    base = (web_url or "").strip().rstrip("/")
    if not base.startswith(("https://", "http://")):
        return None
    return f"{base}{app_path(path)}"


# ------------------------------------------------------------------ the app's pages


def approval_path(approval_id: uuid.UUID) -> str:
    """An approval, open in the unified inbox."""
    return f"/inbox?item={approval_id}"


def decision_path(decision_id: str | uuid.UUID) -> str:
    """A decision record, where its appeals are decided."""
    return f"/decisions/{decision_id}"
