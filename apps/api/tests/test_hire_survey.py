# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""The candidate experience survey (M3) on Postgres, through the API.

* Invited after every outcome (hired, withdrawn, rejected only once the rejection was
  sent), by email, with a signed single use link.
* Anonymous by default: the response cannot be traced to the invite, the candidate or
  the application; a candidate may attach their name.
* Results are totals and averages only, small groups hidden (the service function the
  analytics agent will use); named answers are listed only by the author's choice.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine

from pairly_core.db import create_sessionmaker, set_tenant
from pairly_hire.links import LinkConfig
from pairly_hire.survey import service as survey
from pairly_hire.survey.models import HireSurveyResponse

from .hire_support import HireApi, HireWorld, hire_api, open_job, owner_rows
from .notify_support import LINK_SECRET
from .test_hire_pipeline import deliver, move, other, world
from .test_hire_portal import _apply

__all__ = ["hire_api", "other", "world"]  # fixtures

TOKEN_RE = re.compile(r"/(en|ar)/survey#(sv1\.[A-Za-z0-9._-]+)")
LINKS = LinkConfig(LINK_SECRET, "https://app.pairly.test")


async def invite_all(owner: AsyncEngine, world: HireWorld) -> int:
    session = create_sessionmaker(owner)()
    async with session:
        set_tenant(session, world.org_id, None)
        async with session.begin():
            later = datetime.now(UTC) + survey.DELAY + timedelta(minutes=1)
            return await survey.send_due_invites(session, LINKS, later)


def token_for(mails: list[dict[str, Any]], email: str) -> str:
    for mail in reversed(mails):
        if email in mail["to"]:
            found = TOKEN_RE.search(mail["text"])
            if found:
                return found.group(2)
    raise AssertionError(f"no survey link for {email}")


async def view(api: HireApi, token: str, locale: str = "en") -> Any:
    return await api.client.post(
        "/v1/hire/public/surveys/view",
        json={"token": token, "locale": locale},
        headers=api.public(),
    )


async def answer(api: HireApi, token: str, answers: dict[str, Any], named: bool = False) -> Any:
    return await api.client.post(
        "/v1/hire/public/surveys/answer",
        json={"token": token, "answers": answers, "attach_name": named},
        headers=api.public(),
    )


async def test_invited_after_every_outcome_and_anonymous_by_default(
    hire_api: HireApi, world: HireWorld, other: HireWorld, owner_engine: AsyncEngine, tmp_path: Path
) -> None:
    api = hire_api
    job = await open_job(api, world)
    hired_app, hired_email = await _apply(api, world, job, "Rami Haddad")
    named_app, named_email = await _apply(api, world, job, "Nour Saleh")
    rejected_app, _ = await _apply(api, world, job, "Dana Aziz")
    open_app, _ = await _apply(api, world, job, "Ziad Khoury")
    for app_id in (hired_app, named_app):
        for stage in ("screening", "shortlist", "interview", "offer", "hired"):
            assert (await move(api, world, app_id, stage)).status_code == 200
    assert (await move(api, world, rejected_app, "rejected")).status_code == 200

    # Not before the delay; then the hired ones only: the rejection was not sent yet,
    # and the open application has no outcome.
    session = create_sessionmaker(owner_engine)()
    async with session:
        set_tenant(session, world.org_id, None)
        async with session.begin():
            assert await survey.send_due_invites(session, LINKS) == 0
    assert await invite_all(owner_engine, world) == 2
    assert await invite_all(owner_engine, world) == 0  # once per application
    invited = {
        r.application_id
        for r in await owner_rows(
            owner_engine, world.org_id, "SELECT application_id FROM hire_survey_invites"
        )
    }
    assert invited == {uuid.UUID(hired_app), uuid.UUID(named_app)}
    assert uuid.UUID(open_app) not in invited

    mails = await deliver(api, world, tmp_path)
    token = token_for(mails, hired_email)
    mail = next(m for m in mails if hired_email in m["to"] and "/survey#" in m["text"])
    assert "—" not in mail["text"]

    # The link opens the form without being used; it is bound and single use.
    form = await view(api, token)
    assert form.status_code == 200, form.text
    assert form.headers["cache-control"] == "no-store"
    assert 3 <= len(form.json()["questions"]) <= 5
    arabic = await view(api, token, "ar")
    assert arabic.json()["questions"][0]["label"] != form.json()["questions"][0]["label"]
    parts = token.split(".")
    forged = [
        ".".join([parts[0], other.org_id.hex, *parts[2:]]),
        ".".join([parts[0], parts[1], uuid.uuid4().hex, parts[3]]),
        ".".join([*parts[:3], ("A" if parts[3][0] != "A" else "B") + parts[3][1:]]),
    ]
    for bad in forged:
        assert (await view(api, bad)).status_code == 404
    assert (await answer(api, token, {"communication": 4})).status_code == 400  # overall
    assert (await answer(api, token, {"overall": 9})).status_code == 400
    done = await answer(
        api, token, {"overall": 4, "communication": 3, "recommend": "yes", "comment": "Quick."}
    )
    assert done.status_code == 200, done.text
    assert (await answer(api, token, {"overall": 5})).status_code == 404
    assert (await view(api, token)).status_code == 404

    # Anonymous: nothing in the response points at the invite, the candidate or the
    # application, and only the day is kept.
    (row,) = await owner_rows(owner_engine, world.org_id, "SELECT * FROM hire_survey_responses")
    assert row.named is False
    assert row.candidate_id is None
    assert row.application_id is None
    assert row.answered_on == datetime.now(UTC).date()
    assert set(row._mapping) == {
        "id",
        "org_id",
        "job_id",
        "outcome",
        "locale",
        "answers",
        "comment",
        "named",
        "candidate_id",
        "application_id",
        "answered_on",
    }
    assert "Quick" not in row.comment  # encrypted
    (event,) = await owner_rows(
        owner_engine,
        world.org_id,
        "SELECT actor_id, payload FROM audit_events WHERE action = 'hire.survey.answered'",
    )
    assert event.actor_id == "hire_survey:anonymous"
    assert str(row.id) not in str(event.payload)

    # A candidate may attach their name; only those answers are listed.
    named_token = token_for(mails, named_email)
    signed = await answer(api, named_token, {"overall": 2, "comment": "Slow replies."}, True)
    assert signed.status_code == 200
    listed = await api.call("GET", "/v1/hire/surveys/named", world, "recruiter")
    assert listed.status_code == 200, listed.text
    (named,) = listed.json()
    assert named["application_id"] == named_app
    assert named["comment"] == "Slow replies."

    # Results: totals only, small groups hidden.
    results = await api.call("GET", "/v1/hire/surveys/results", world, "recruiter")
    assert results.status_code == 200, results.text
    got = results.json()
    assert (got["invited"], got["responses"], got["response_rate"]) == (2, 2, 1.0)
    assert got["overall"]["suppressed"] is True
    assert got["overall"]["averages"] == {}
    for who in ("manager", "interviewer"):
        denied = await api.call("GET", "/v1/hire/surveys/results", world, who)
        assert denied.status_code == 403, who


async def test_aggregate_results_for_the_analytics_agent(
    hire_api: HireApi, world: HireWorld, owner_engine: AsyncEngine
) -> None:
    job = await open_job(hire_api, world)
    session = create_sessionmaker(owner_engine)()
    async with session:
        set_tenant(session, world.org_id, None)
        async with session.begin():
            for n, (overall, outcome) in enumerate(
                [
                    (5, "hired"),
                    (4, "hired"),
                    (4, "hired"),
                    (3, "hired"),
                    (2, "hired"),
                    (1, "rejected"),
                ]
            ):
                session.add(
                    HireSurveyResponse(
                        id=uuid.uuid4(),
                        org_id=world.org_id,
                        job_id=uuid.UUID(job["id"]),
                        outcome=outcome,
                        locale="en",
                        answers={"overall": overall, "recommend": "yes" if n % 2 else "no"},
                        named=False,
                        answered_on=date.today(),
                    )
                )
        async with session.begin():
            results = await survey.aggregate_results(session, job_id=uuid.UUID(job["id"]))
    assert results.responses == 6
    assert results.overall.suppressed is False
    assert results.overall.averages["overall"] == round((5 + 4 + 4 + 3 + 2 + 1) / 6, 2)
    assert results.overall.recommend == {"yes": 3, "no": 3, "unsure": 0}
    assert results.by_outcome["hired"].averages["overall"] == 3.6
    assert results.by_outcome["rejected"].suppressed is True  # one answer: hidden
    assert results.by_outcome["rejected"].averages == {}
