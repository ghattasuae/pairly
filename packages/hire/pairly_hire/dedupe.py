# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
# ruff: noqa: RUF001  (Arabic letters ruff mistakes for Latin look-alikes)
"""Duplicate applicant detection across jobs (brief 3.2.4). Hints for a person, never merges.

Signals, all computed without plaintext lookups (personal fields are encrypted):

* ``email``: one candidate row per email blind index, so the same address applying
  again (to another job, or the same job: ``same_job``) is the same candidate.
* ``phone``: another candidate with the same phone blind index.
* ``cv_hash``: another application whose CV has the same SHA-256. Each application keeps
  its own document row (security review 2026-10, L-09), so the bytes are compared, never
  the row.
* ``name``: another candidate whose name shares a word blind index, confirmed by a
  fuzzy comparison of the decrypted, normalised names (``NAME_THRESHOLD``). Catches
  reordered names, a dropped middle name, and small spelling differences.

The result is stored on the application (``duplicate_signals``) and sets
``needs_human_review``. Nothing is merged or rejected automatically.
"""

from __future__ import annotations

import re
import unicodedata
import uuid
from difflib import SequenceMatcher
from typing import Any, Final

from sqlalchemy import ARRAY, Text, literal, select
from sqlalchemy.ext.asyncio import AsyncSession

from pairly_core.crypto.blind_index import blind_index
from pairly_core.crypto.types import unseal
from pairly_core.ingestion.models import Document
from pairly_hire.models import ApplicationStatus, HireApplication, HireCandidate

WORKFLOW: Final = "hire"
NAME_THRESHOLD: Final = 0.85
MAX_NAME_CANDIDATES: Final = 50

_TASHKEEL = re.compile(r"[ؐ-ًؚ-ٰٟۖ-ۭـ]")
_NON_WORD = re.compile(r"[^\w]+", re.UNICODE)
_AR_FOLD = str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ٱ": "ا", "ة": "ه", "ى": "ي"})


def normalise_name(name: str) -> str:
    text = unicodedata.normalize("NFKC", name).casefold()
    text = _TASHKEEL.sub("", text).translate(_AR_FOLD)
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    return " ".join(t for t in _NON_WORD.sub(" ", text).split() if len(t) > 1)


def name_tokens(name: str) -> list[str]:
    return sorted(set(normalise_name(name).split()))


def name_bidx(name: str, org_id: uuid.UUID) -> list[str]:
    indexes = (blind_index("text", token, org_id) for token in name_tokens(name))
    return sorted({i for i in indexes if i is not None})


def name_similarity(a: str, b: str) -> float:
    left, right = " ".join(name_tokens(a)), " ".join(name_tokens(b))
    if not left or not right:
        return 0.0
    tokens_a, tokens_b = set(left.split()), set(right.split())
    # A dropped middle name: every word of the shorter name is in the longer one.
    if len(tokens_a & tokens_b) >= 2 and (tokens_a <= tokens_b or tokens_b <= tokens_a):
        return 1.0
    return SequenceMatcher(None, left, right).ratio()


class _Signals:
    def __init__(self) -> None:
        self.by_app: dict[uuid.UUID, dict[str, Any]] = {}

    def add(self, app: HireApplication, signal: str) -> None:
        entry = self.by_app.setdefault(
            app.id,
            {"application_id": str(app.id), "candidate_id": str(app.candidate_id), "signals": []},
        )
        if signal not in entry["signals"]:
            entry["signals"].append(signal)

    def result(self) -> list[dict[str, Any]]:
        out = list(self.by_app.values())
        for entry in out:
            entry["signals"].sort()
        return sorted(out, key=lambda e: (e["candidate_id"], e["application_id"]))


async def find_duplicates(
    session: AsyncSession, app: HireApplication, candidate: HireCandidate
) -> list[dict[str, Any]]:
    """Other submitted applications that may be the same person. See the module docstring."""
    signals = _Signals()
    submitted = HireApplication.status != ApplicationStatus.INCOMPLETE.value
    others = HireApplication.id != app.id

    # Same email: the same candidate row.
    for other in await session.scalars(
        select(HireApplication).where(
            HireApplication.candidate_id == candidate.id, others, submitted
        )
    ):
        signals.add(other, "email")
        if other.job_id == app.job_id:
            signals.add(other, "same_job")

    if candidate.phone_bidx is not None:
        phone_ids = select(HireCandidate.id).where(
            HireCandidate.phone_bidx == candidate.phone_bidx, HireCandidate.id != candidate.id
        )
        for other in await session.scalars(
            select(HireApplication).where(
                HireApplication.candidate_id.in_(phone_ids), others, submitted
            )
        ):
            signals.add(other, "phone")

    if app.cv_document_id is not None:
        same_bytes = select(Document.id).where(
            Document.sha256
            == select(Document.sha256).where(Document.id == app.cv_document_id).scalar_subquery(),
            Document.workflow == WORKFLOW,
        )
        for other in await session.scalars(
            select(HireApplication).where(
                HireApplication.cv_document_id.in_(same_bytes), others, submitted
            )
        ):
            signals.add(other, "cv_hash")

    tokens = list(candidate.name_bidx or [])
    if tokens:
        own_name = str(unseal(candidate, "full_name") or "")
        similar = await session.scalars(
            select(HireCandidate)
            .where(
                HireCandidate.id != candidate.id,
                HireCandidate.name_bidx.op("?|")(literal(tokens, ARRAY(Text))),
            )
            .order_by(HireCandidate.created_at.desc())
            .limit(MAX_NAME_CANDIDATES)
        )
        matched = [
            c.id
            for c in similar
            if name_similarity(own_name, str(unseal(c, "full_name") or "")) >= NAME_THRESHOLD
        ]
        if matched:
            for other in await session.scalars(
                select(HireApplication).where(
                    HireApplication.candidate_id.in_(matched), others, submitted
                )
            ):
                signals.add(other, "name")
    return signals.result()
