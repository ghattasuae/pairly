# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""JobPosting JSON-LD validity, the JSON and XML feeds, and the anti-spam helpers."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from defusedxml import ElementTree

from pairly_core.config import Environment
from pairly_core.errors import ServiceUnavailableError
from pairly_hire import antispam
from pairly_hire.antispam import (
    DUMMY_TOKEN,
    CloudflareTurnstileVerifier,
    FakeTurnstileVerifier,
    RateLimitedError,
    SlidingWindowLimiter,
    is_disposable,
    turnstile_from_env,
)
from pairly_hire.careers import CareersOrg, description_html, job_posting_ld, json_feed, xml_feed
from pairly_hire.models import HireJob

ORG = CareersOrg(id=uuid.uuid4(), name="Acme & Sons <Logistics>", slug="acme")
POSTED = datetime(2026, 10, 1, 9, tzinfo=UTC)


def job(**overrides: Any) -> HireJob:
    values: dict[str, Any] = {
        "id": uuid.uuid4(),
        "org_id": ORG.id,
        "slug": "senior-backend-engineer",
        "title": "Senior Backend Engineer",
        "title_ar": "مهندس/ة خلفيات أول",
        "team": "Platform",
        "location": "Dubai",
        "country": "AE",
        "work_model": "hybrid",
        "employment_type": "full_time",
        "salary_min": Decimal("30000.00"),
        "salary_max": Decimal("38000.00"),
        "salary_currency": "AED",
        "salary_period": "month",
        "show_salary": True,
        "description_en": "About the role\nBuild <services>.\n\nWhat you need\n- Python\n- SQL",
        "description_ar": "عن الوظيفة",
        "status": "open",
        "published_at": POSTED,
        "created_at": POSTED,
    }
    values.update(overrides)
    return HireJob(**values)


# Google's required and recommended JobPosting properties (developers.google.com).
REQUIRED = ("title", "description", "datePosted", "hiringOrganization")


def assert_valid_job_posting(data: dict[str, Any]) -> None:
    assert data["@context"].startswith("https://schema.org")
    assert data["@type"] == "JobPosting"
    for key in REQUIRED:
        assert data[key], key
    assert data["hiringOrganization"]["@type"] == "Organization"
    datetime.fromisoformat(data["datePosted"])
    assert data["employmentType"] in {
        "FULL_TIME",
        "PART_TIME",
        "CONTRACTOR",
        "TEMPORARY",
        "INTERN",
    }
    if data.get("jobLocationType") == "TELECOMMUTE":
        assert data["applicantLocationRequirements"]["@type"] == "Country"
    else:
        address = data["jobLocation"]["address"]
        assert data["jobLocation"]["@type"] == "Place"
        assert address["@type"] == "PostalAddress"
        assert address["addressLocality"]
    if "baseSalary" in data:
        value = data["baseSalary"]["value"]
        assert data["baseSalary"]["@type"] == "MonetaryAmount"
        assert value["minValue"] <= value["maxValue"]
        assert value["unitText"] in {"HOUR", "DAY", "WEEK", "MONTH", "YEAR"}
    json.dumps(data)  # serialisable


def test_job_posting_onsite_with_salary() -> None:
    data = job_posting_ld(job(), ORG, "https://jobs.example/en/careers/acme/x")
    assert_valid_job_posting(data)
    assert data["baseSalary"]["currency"] == "AED"
    assert data["baseSalary"]["value"]["unitText"] == "MONTH"
    assert data["jobLocation"]["address"]["addressCountry"] == "AE"
    assert data["datePosted"] == "2026-10-01"
    assert data["directApply"] is True


def test_job_posting_hides_salary_when_the_job_does() -> None:
    data = job_posting_ld(job(show_salary=False), ORG, "https://jobs.example/x")
    assert_valid_job_posting(data)
    assert "baseSalary" not in data


def test_job_posting_remote() -> None:
    data = job_posting_ld(job(work_model="remote"), ORG, "https://jobs.example/x")
    assert_valid_job_posting(data)
    assert data["jobLocationType"] == "TELECOMMUTE"
    assert data["applicantLocationRequirements"]["name"] == "AE"
    assert "jobLocation" not in data


def test_description_html_escapes_and_lists() -> None:
    out = description_html("Build <script>alert(1)</script>\n- one\n- two & three\nend")
    assert "<script>" not in out
    assert "&lt;script&gt;" in out
    assert "<ul><li>one</li><li>two &amp; three</li></ul>" in out
    assert out.endswith("<p>end</p>")


def test_json_feed() -> None:
    jobs = [job(), job(slug="two", show_salary=False)]
    feed = json_feed(ORG, jobs, "https://jobs.example", POSTED)
    assert feed["version"] == 1
    assert feed["organisation"]["careers_url"] == "https://jobs.example/en/careers/acme"
    first, second = feed["jobs"]
    assert first["salary"] == {
        "min": "30000.00",
        "max": "38000.00",
        "currency": "AED",
        "period": "month",
    }
    assert second["salary"] is None
    assert first["url"] == "https://jobs.example/en/careers/acme/senior-backend-engineer"
    assert first["employment_type_schema_org"] == "FULL_TIME"
    json.dumps(feed)


def test_xml_feed_is_well_formed_and_escaped() -> None:
    text = xml_feed(
        ORG,
        [job(title="Engineer <R&D>\x01", show_salary=False), job(slug="b")],
        "https://jobs.example",
        POSTED,
    )
    root = ElementTree.fromstring(text.encode("utf-8"))
    assert root.tag == "source"
    assert root.findtext("publisher") == "Acme & Sons <Logistics>"
    jobs = root.findall("job")
    assert len(jobs) == 2
    assert jobs[0].findtext("title") == "Engineer <R&D>"
    assert jobs[0].find("salary") is None
    assert jobs[1].findtext("salary") == "30000.00-38000.00 AED per month"
    assert jobs[1].findtext("jobtype") == "FULL_TIME"


# ------------------------------------------------------------------ anti-spam


@pytest.mark.parametrize(
    ("email", "disposable"),
    [
        ("someone@mailinator.com", True),
        ("someone@SUB.Mailinator.com", True),
        ("x@yopmail.fr", True),
        ("person@example.test", False),
        ("person@gmail.com", False),
        ("person@notmailinator.com", False),
    ],
)
def test_disposable_domains(email: str, disposable: bool) -> None:
    assert is_disposable(email) is disposable


def test_disposable_list_is_small_lowercase_and_licensed() -> None:
    from importlib.resources import files

    text = files("pairly_hire").joinpath("data", "disposable_domains.txt").read_text("utf-8")
    assert "CC0-1.0" in text.splitlines()[0]
    domains = antispam.disposable_domains()
    assert 20 < len(domains) < 1000
    assert all(d == d.lower() and "." in d for d in domains)


def test_sliding_window_limiter() -> None:
    now = [0.0]
    limiter = SlidingWindowLimiter(2, 60, clock=lambda: now[0])
    limiter.hit("a")
    limiter.hit("a")
    limiter.hit("b")
    with pytest.raises(RateLimitedError) as caught:
        limiter.hit("a")
    assert caught.value.headers["Retry-After"] == "60"
    now[0] = 61.0
    limiter.hit("a")


async def test_fake_turnstile_accepts_only_the_dummy_token() -> None:
    fake = FakeTurnstileVerifier()
    assert await fake.verify(DUMMY_TOKEN, "198.51.100.1")
    assert not await fake.verify("anything-else", None)
    assert not await fake.verify("", None)


def test_turnstile_fails_closed_in_production(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(antispam.TURNSTILE_ENV, raising=False)
    monkeypatch.delenv(antispam.TURNSTILE_SECRET_ENV, raising=False)
    assert isinstance(turnstile_from_env(Environment.TEST), FakeTurnstileVerifier)
    with pytest.raises(ServiceUnavailableError):
        turnstile_from_env(Environment.PRODUCTION)
    monkeypatch.setenv(antispam.TURNSTILE_ENV, "cloudflare")
    with pytest.raises(ServiceUnavailableError):
        turnstile_from_env(Environment.TEST)
    monkeypatch.setenv(antispam.TURNSTILE_SECRET_ENV, "secret-value")
    assert isinstance(turnstile_from_env(Environment.PRODUCTION), CloudflareTurnstileVerifier)


async def test_cloudflare_verifier_never_calls_out_in_tests() -> None:
    import httpx

    seen: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.content)
        return httpx.Response(200, json={"success": request.content.count(b"good") == 1})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    verifier = CloudflareTurnstileVerifier("secret", client=client)
    assert await verifier.verify("good", "198.51.100.1")
    assert not await verifier.verify("bad", None)
    assert not await verifier.verify("", None)

    def broken(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    failing = CloudflareTurnstileVerifier(
        "secret", client=httpx.AsyncClient(transport=httpx.MockTransport(broken))
    )
    assert not await failing.verify("good", None)  # fail closed
    assert len(seen) == 2
