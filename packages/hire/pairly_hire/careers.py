# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Public views of open jobs: the careers page data, ``JobPosting`` JSON-LD and job feeds.

Only open jobs of an organisation whose careers page is visible are ever shown
(``hire_careers_org(slug)``, a SECURITY DEFINER function of migration ``hire_0001``
that returns the organisation's id and name and nothing else). Public data is the job
text, location, work model, employment type, team, and the salary range only when the
job shows it (pay transparency). Never the rubric, never anything about applications.

* ``job_posting_ld``: schema.org ``JobPosting`` for Google Jobs (brief 3.1.6). Remote
  jobs use ``jobLocationType: TELECOMMUTE`` with the country as the applicant location
  requirement; others a ``Place`` with locality and country.
* ``json_feed`` and ``xml_feed``: the open jobs for job boards. The XML follows the
  widely used ``<source><job>`` layout; every value is escaped and characters XML does
  not allow are removed.
"""

from __future__ import annotations

import html
import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from pairly_hire.models import HireJob, WorkModel

EMPLOYMENT_TYPES: Final = {
    "full_time": "FULL_TIME",
    "part_time": "PART_TIME",
    "contract": "CONTRACTOR",
    "temporary": "TEMPORARY",
    "internship": "INTERN",
}
SALARY_UNITS: Final = {"year": "YEAR", "month": "MONTH", "hour": "HOUR"}
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")
_XML_INVALID = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]")


@dataclass(frozen=True, slots=True)
class CareersOrg:
    id: uuid.UUID
    name: str
    slug: str


async def careers_org(session: AsyncSession, slug: str) -> CareersOrg | None:
    """The organisation behind a public careers slug, or ``None``. Opens a transaction
    on ``session`` with no tenant (the caller commits it before binding the tenant)."""
    if not _SLUG_RE.fullmatch(slug):
        return None
    row = (
        await session.execute(
            text("SELECT org_id, org_name FROM hire_careers_org(:slug)"), {"slug": slug}
        )
    ).first()
    if row is None or row.org_id is None:
        return None
    return CareersOrg(id=row.org_id, name=str(row.org_name), slug=slug)


def job_url(web_url: str, locale: str, org_slug: str, job_slug: str) -> str:
    return f"{web_url.rstrip('/')}/{locale}/careers/{org_slug}/{job_slug}"


def salary(job: HireJob) -> dict[str, Any] | None:
    """The public salary range, only when the job shows it."""
    if not job.show_salary or job.salary_min is None or job.salary_max is None:
        return None
    return {
        "min": str(job.salary_min),
        "max": str(job.salary_max),
        "currency": job.salary_currency,
        "period": job.salary_period,
    }


def description_html(plain: str) -> str:
    """Plain job text as simple HTML: paragraphs, and ``- `` lines as lists. Escaped."""
    out: list[str] = []
    items: list[str] = []

    def flush_items() -> None:
        if items:
            out.append("<ul>" + "".join(f"<li>{i}</li>" for i in items) + "</ul>")
            items.clear()

    for raw in plain.splitlines():
        line = raw.strip()
        if line.startswith("- "):
            items.append(html.escape(line[2:].strip()))
            continue
        flush_items()
        if line:
            out.append(f"<p>{html.escape(line)}</p>")
    flush_items()
    return "".join(out)


def job_posting_ld(job: HireJob, org: CareersOrg, url: str) -> dict[str, Any]:
    posted = job.published_at or job.created_at
    data: dict[str, Any] = {
        "@context": "https://schema.org/",
        "@type": "JobPosting",
        "title": job.title,
        "description": description_html(job.description_en),
        "datePosted": posted.date().isoformat(),
        "employmentType": EMPLOYMENT_TYPES[job.employment_type],
        "hiringOrganization": {"@type": "Organization", "name": org.name},
        "identifier": {"@type": "PropertyValue", "name": org.name, "value": job.slug},
        "directApply": True,
        "url": url,
    }
    if job.work_model == WorkModel.REMOTE.value:
        data["jobLocationType"] = "TELECOMMUTE"
        if job.country:
            data["applicantLocationRequirements"] = {"@type": "Country", "name": job.country}
    else:
        address: dict[str, Any] = {"@type": "PostalAddress", "addressLocality": job.location}
        if job.country:
            address["addressCountry"] = job.country
        data["jobLocation"] = {"@type": "Place", "address": address}
    pay = salary(job)
    if pay is not None:
        data["baseSalary"] = {
            "@type": "MonetaryAmount",
            "currency": pay["currency"],
            "value": {
                "@type": "QuantitativeValue",
                "minValue": float(pay["min"]),
                "maxValue": float(pay["max"]),
                "unitText": SALARY_UNITS[str(pay["period"])],
            },
        }
    return data


def public_job(job: HireJob, org: CareersOrg, web_url: str, locale: str = "en") -> dict[str, Any]:
    return {
        "slug": job.slug,
        "title": job.title,
        "title_ar": job.title_ar,
        "team": job.team,
        "location": job.location,
        "country": job.country,
        "work_model": job.work_model,
        "employment_type": job.employment_type,
        "salary": salary(job),
        "published_at": (job.published_at or job.created_at).isoformat(),
        "url": job_url(web_url, locale, org.slug, job.slug),
    }


def json_feed(
    org: CareersOrg, jobs: Sequence[HireJob], web_url: str, now: datetime
) -> dict[str, Any]:
    return {
        "version": 1,
        "organisation": {
            "name": org.name,
            "careers_url": f"{web_url.rstrip('/')}/en/careers/{org.slug}",
        },
        "generated_at": now.isoformat(),
        "jobs": [
            public_job(job, org, web_url)
            | {
                "description_en": job.description_en,
                "description_ar": job.description_ar,
                "employment_type_schema_org": EMPLOYMENT_TYPES[job.employment_type],
            }
            for job in jobs
        ],
    }


def _x(value: object) -> str:
    text_value = _XML_INVALID.sub("", "" if value is None else str(value))
    return html.escape(text_value, quote=True)


def xml_feed(org: CareersOrg, jobs: Sequence[HireJob], web_url: str, now: datetime) -> str:
    lines = [
        '<?xml version="1.0" encoding="utf-8"?>',
        "<source>",
        f"  <publisher>{_x(org.name)}</publisher>",
        f"  <publisherurl>{_x(web_url.rstrip('/') + '/en/careers/' + org.slug)}</publisherurl>",
        f"  <lastBuildDate>{_x(now.isoformat())}</lastBuildDate>",
    ]
    for job in jobs:
        pay = salary(job)
        lines += [
            "  <job>",
            f"    <title>{_x(job.title)}</title>",
            f"    <date>{_x((job.published_at or job.created_at).date().isoformat())}</date>",
            f"    <referencenumber>{_x(job.slug)}</referencenumber>",
            f"    <url>{_x(job_url(web_url, 'en', org.slug, job.slug))}</url>",
            f"    <company>{_x(org.name)}</company>",
            f"    <city>{_x(job.location)}</city>",
            f"    <country>{_x(job.country or '')}</country>",
            f"    <jobtype>{_x(EMPLOYMENT_TYPES[job.employment_type])}</jobtype>",
            f"    <remotetype>{_x(job.work_model)}</remotetype>",
            f"    <description>{_x(job.description_en)}</description>",
        ]
        if job.team:
            lines.append(f"    <category>{_x(job.team)}</category>")
        if pay is not None:
            lines.append(
                f"    <salary>{_x(pay['min'])}-{_x(pay['max'])} {_x(pay['currency'])} "
                f"per {_x(pay['period'])}</salary>"
            )
        lines.append("  </job>")
    lines.append("</source>")
    return "\n".join(lines) + "\n"
