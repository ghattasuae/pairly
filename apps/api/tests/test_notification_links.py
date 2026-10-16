# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Every link a notification carries opens a page that exists (offline, no database).

* in-app links are app paths without a locale (``/inbox?item=<id>``): what the bell
  follows; full URLs, other hosts and tricks (``//host``, dot segments) are refused;
* the path builders of every workflow (approvals, appeals, Pay tickets, Hire pipeline
  and kit, expense, data requests) map to a page of the web app;
* every ``url`` variable of every system template has a sample that maps to a page of
  the web app (the samples show what the real links look like);
* the digest email turns an item's app path into a full link.

The pages are read from the web app's file routes (``apps/web/src/app/[locale]``).
"""

from __future__ import annotations

import importlib
import re
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from pairly_core.notifications import delivery
from pairly_core.notifications.defaults import DefaultTemplate
from pairly_core.notifications.links import (
    absolute_url,
    approval_path,
    decision_path,
    is_app_path,
)
from pairly_expense.graph import NOTES_PATH
from pairly_hire.links import kit_path, pipeline_path
from pairly_hire.portal.notices import DATA_REQUESTS_PAGE
from pairly_pay.alerts import ticket_path

APP = Path(__file__).resolve().parents[2] / "web" / "src" / "app" / "[locale]"
LOCALES = ("en", "ar")
EXTERNAL_HOSTS = {"meet.pairly.example"}  # a meeting link is the provider's page

TEMPLATE_MODULES = (
    "pairly_core.notifications.defaults",
    "pairly_expense.templates",
    "pairly_pay.templates",
    "pairly_hire.templates",
    "pairly_hire.scheduling.templates",
    "pairly_hire.portal.notices",
    "pairly_hire.portal.templates",
    "pairly_hire.references.templates",
    "pairly_hire.interviewing.templates",
    "pairly_hire.interviewing.template_seed",
    "pairly_hire.survey.templates",
    "pairly_hire.messaging.library",
)


def _routes() -> list[re.Pattern[str]]:
    patterns: list[re.Pattern[str]] = []
    for page in APP.rglob("page.tsx"):
        parts = [p for p in page.parent.relative_to(APP).parts if not p.startswith("(")]
        if any(p.startswith("[...") for p in parts):
            continue  # the catch-all not-found page
        regex = "/".join("[^/]+" if p.startswith("[") else re.escape(p) for p in parts)
        patterns.append(re.compile(f"^/{regex}$" if regex else "^/$"))
    return patterns


ROUTES = _routes()


def page_exists(link: str) -> bool:
    """Whether the web app serves a page at ``link`` (a path or a full URL)."""
    path = urlsplit(link).path.rstrip("/") or "/"
    segments = path.split("/")
    if len(segments) > 1 and segments[1] in LOCALES:
        path = "/" + "/".join(segments[2:])
    path = path.rstrip("/") or "/"
    return any(p.match(path) for p in ROUTES)


def _templates() -> list[DefaultTemplate]:
    found: dict[int, DefaultTemplate] = {}
    for name in TEMPLATE_MODULES:
        module = importlib.import_module(name)
        for value in vars(module).values():
            items = value if isinstance(value, tuple | list) else (value,)
            for item in items:
                if isinstance(item, DefaultTemplate):
                    found[id(item)] = item
    return list(found.values())


def test_the_route_table_is_read() -> None:
    assert len(ROUTES) > 30
    assert page_exists("/inbox")
    assert page_exists("/en/pay/bank-changes/abc")
    assert not page_exists("/approvals/abc")  # no such page: the old approval link
    assert not page_exists("/appeals/abc")
    assert not page_exists("/expense/notes/abc")
    assert not page_exists("/does-not-exist")


ID = uuid.UUID("3f2a9c1d-5b7e-4c2a-9d1f-0a1b2c3d4e5f")


@pytest.mark.parametrize(
    "path",
    [
        approval_path(ID),
        decision_path("dec_01JABCDEFGHJKMNPQRSTVWXYZ0"),
        ticket_path(ID),
        pipeline_path(ID),
        pipeline_path(ID, sla=True),
        pipeline_path(ID, candidate=ID),
        kit_path(ID),
        NOTES_PATH,
        DATA_REQUESTS_PAGE,
        f"{DATA_REQUESTS_PAGE}?request={ID}",
    ],
)
def test_every_in_app_link_is_an_app_path_to_a_page(path: str) -> None:
    assert is_app_path(path), path
    assert page_exists(path), path
    assert absolute_url("https://app.test", path) == f"https://app.test{path}"


def test_approvals_open_in_the_inbox() -> None:
    assert approval_path(ID) == f"/inbox?item={ID}"


@pytest.mark.parametrize(
    "link",
    [
        "https://app.pairly.example/inbox",
        "http://evil.example/en/inbox",
        "//evil.example/inbox",
        "/\\evil.example/inbox",
        "/inbox/../settings",
        "/inbox#frag",
        "javascript:alert(1)",
        "inbox",
        "",
        "/inbox?item=<x>",
    ],
)
def test_anything_but_an_app_path_is_refused(link: str) -> None:
    assert not is_app_path(link)


def test_every_template_url_maps_to_a_page() -> None:
    templates = _templates()
    assert len({t.key for t in templates}) > 15
    checked = 0
    for t in templates:
        for name, spec in t.variables.items():
            if spec.get("type") != "url" or "sample" not in spec:
                continue
            sample = str(spec["sample"])
            if urlsplit(sample).hostname in EXTERNAL_HOSTS:
                continue
            assert page_exists(sample), (t.key, t.channel, t.locale, name, sample)
            checked += 1
    assert checked > 20


def test_the_digest_turns_app_paths_into_full_links(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PAIRLY_PUBLIC_WEB_URL", "https://app.test/")
    assert delivery._digest_url(f"/inbox?item={ID}") == f"https://app.test/inbox?item={ID}"
    assert delivery._digest_url("https://old.test/approvals/x") == "https://old.test/approvals/x"
    assert delivery._digest_url("//evil.example/x") == ""
    assert delivery._digest_url(None) == ""
    monkeypatch.delenv("PAIRLY_PUBLIC_WEB_URL")
    assert delivery._digest_url("/inbox") == ""
