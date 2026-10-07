# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Fixtures for extraction and risk unit tests: no database, no network.

The model router runs with scripted ``FakeProvider``s, an in-memory call store and a
budget stub, exactly like the router's own unit tests. All documents are synthetic.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Annotated, Any, ClassVar, Literal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from pairly_core.db import TenantSession
from pairly_core.documents.model import BBox, NormalizedDocument, Page, ParserInfo, TextBlock, Word
from pairly_core.extraction.types import (
    Extracted,
    ExtractionModel,
    Iban,
    Money,
    Percent,
    Rule,
    TrnAE,
)
from pairly_core.llm.catalog import load_catalog, load_prices
from pairly_core.llm.prompt_registry import PromptRegistry
from pairly_core.llm.providers.fake import FakeProvider
from pairly_core.llm.resilience import RetryPolicy
from pairly_core.llm.router import ModelRouter
from pairly_core.llm.settings import LLMSettings, ModelMode
from pairly_core.llm.store import OUTPUT_STATUSES, StoredCall

ORG = uuid.UUID("00000000-0000-4000-8000-0000000000e1")
HAIKU = "claude-haiku-4-5-20251001"
SONNET = "claude-sonnet-5-5"
LUNA = "gpt-6-luna"
SOL = "gpt-6.1-sol"
IBAN = "AE070331234567890123456"

INVOICE_LINES = [
    "Desert Falcon Trading LLC",
    "Tax Invoice",
    "Invoice No: INV-2026-0042",
    "Invoice Date: 07/10/2026",
    "TRN: 100123456700003",
    "Bank IBAN: AE07 0331 2345 6789 0123 456",
    "Item Qty Unit Price Total",
    "Printer paper A4 10 25.00 250.00",
    "Toner cartridge 2 1,000.00 2,000.00",
    "Subtotal: AED 2,250.00",
    "VAT 5%: AED 112.50",
    "Total: AED 2,362.50",
]


def make_page(number: int, lines: list[str]) -> Page:
    blocks: list[TextBlock] = []
    words: list[Word] = []
    for i, line in enumerate(lines):
        top = 72.0 + i * 14.0
        blocks.append(
            TextBlock(
                text=line,
                bbox=BBox(x0=72.0, top=top, x1=72.0 + 6.0 * len(line), bottom=top + 12.0),
                source="pdf",
            )
        )
        x = 72.0
        for token in line.split(" "):
            words.append(
                Word(text=token, bbox=BBox(x0=x, top=top, x1=x + 6.0 * len(token), bottom=top + 12))
            )
            x += 6.0 * (len(token) + 1)
    return Page(
        number=number,
        width=612.0,
        height=792.0,
        unit="pt",
        text="\n".join(lines),
        blocks=blocks,
        words=words,
    )


def make_doc(*pages: list[str], warnings: list[Any] | None = None) -> NormalizedDocument:
    return NormalizedDocument(
        media_type="application/pdf",
        parser=ParserInfo(name="synthetic", version="1"),
        pages=[make_page(i + 1, lines) for i, lines in enumerate(pages)],
        warnings=warnings or [],
    )


@pytest.fixture
def invoice_doc() -> NormalizedDocument:
    return make_doc(INVOICE_LINES)


class Line(ExtractionModel):
    description: Annotated[Extracted[str], Rule(required=True)]
    quantity: Annotated[Extracted[Decimal], Rule(required=True)]
    line_total: Annotated[Extracted[Money], Rule(required=True, default_currency="AED")]


class Invoice(ExtractionModel):
    schema_name: ClassVar[str] = "pay.test_invoice"
    schema_version: ClassVar[str] = "1"
    default_threshold: ClassVar[float] = 0.7

    invoice_number: Annotated[Extracted[str], Rule(required=True, threshold=0.8)]
    invoice_date: Annotated[Extracted[date], Rule(required=True)]
    vendor_trn: Extracted[TrnAE]
    bank_iban: Extracted[Iban]
    currency: Extracted[Literal["AED", "USD", "EUR"]]
    vat_rate: Extracted[Percent]
    total: Annotated[Extracted[Money], Rule(required=True, threshold=0.9)]
    line_items: Annotated[list[Line], Rule(required=True, max_items=50)]


def answer(
    path: str, value: str | None, quote: str | None, confidence: float = 0.95
) -> dict[str, Any]:
    return {"path": path, "value": value, "quote": quote, "confidence": confidence}


GOOD_FIELDS = [
    answer("invoice_number", "INV-2026-0042", "Invoice No: INV-2026-0042"),
    answer("invoice_date", "2026-10-07", "Invoice Date: 07/10/2026"),
    answer("vendor_trn", "100123456700003", "TRN: 100123456700003"),
    answer("bank_iban", IBAN, "AE07 0331 2345 6789 0123 456"),
    answer("currency", "AED", "Total: AED 2,362.50"),
    answer("vat_rate", "5", "VAT 5%"),
    answer("total", "AED 2362.50", "Total: AED 2,362.50"),
    answer("line_items[0].description", "Printer paper A4", "Printer paper A4"),
    answer("line_items[0].quantity", "10", "Printer paper A4 10"),
    answer("line_items[0].line_total", "AED 250.00", "25.00 250.00"),
    answer("line_items[1].description", "Toner cartridge", "Toner cartridge"),
    answer("line_items[1].quantity", "2", "Toner cartridge 2"),
    answer("line_items[1].line_total", "AED 2000.00", "1,000.00 2,000.00"),
]


def output(fields: list[dict[str, Any]], confidence: float = 0.9) -> dict[str, Any]:
    return {"fields": fields, "confidence": confidence}


# ------------------------------------------------------------------ router harness


class MemoryStore:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    async def insert(self, session: AsyncSession, values: dict[str, Any]) -> uuid.UUID:
        call_id: uuid.UUID = values.setdefault("id", uuid.uuid4())
        self.rows.append(dict(values))
        return call_id

    def _stored(self, row: dict[str, Any]) -> StoredCall:
        return StoredCall(
            id=row["id"],
            chain_id=row["chain_id"],
            task=row["task"],
            prompt_version=row["prompt_version"],
            prompt_hash=row["prompt_hash"],
            provider=row["provider"],
            model=row["model"],
            tier=row["tier"],
            schema_name=row["schema_name"],
            schema_hash=row["schema_hash"],
            request_hash=row["request_hash"],
            mode=row["mode"],
            status=row["status"],
            request=row["request"],
            response=row.get("response"),
            output_complete=row.get("output_complete", True),
            stop_reason=row.get("stop_reason"),
            confidence=row.get("confidence"),
        )

    async def find_recording(self, session: AsyncSession, request_hash: str) -> StoredCall | None:
        statuses = {s.value for s in OUTPUT_STATUSES}
        for row in reversed(self.rows):
            if row["request_hash"] == request_hash and row["status"] in statuses:
                return self._stored(row)
        return None

    async def get(self, session: AsyncSession, call_id: uuid.UUID) -> StoredCall | None:
        for row in self.rows:
            if row["id"] == call_id:
                return self._stored(row)
        return None


class StubBudget:
    async def reserve(self, session: AsyncSession, org_id: uuid.UUID, amount: int) -> uuid.UUID:
        return uuid.uuid4()

    async def release(self, session: AsyncSession, org_id: uuid.UUID, rid: uuid.UUID) -> None:
        return None

    async def settle(
        self, session: AsyncSession, org_id: uuid.UUID, rid: uuid.UUID | None, cost: int
    ) -> None:
        return None


@dataclass
class Harness:
    router: ModelRouter
    anthropic: FakeProvider
    openai: FakeProvider
    store: MemoryStore


def make_harness(
    mode: ModelMode = ModelMode.LIVE,
    *,
    store: MemoryStore | None = None,
    anthropic: FakeProvider | None = None,
    openai: FakeProvider | None = None,
) -> Harness:
    prices = load_prices()
    anthropic = anthropic or FakeProvider("anthropic")
    openai = openai or FakeProvider("openai")
    store = store or MemoryStore()

    async def no_sleep(seconds: float) -> None:
        return None

    router = ModelRouter(
        settings=LLMSettings(model_mode=mode),
        providers={"anthropic": anthropic, "openai": openai},
        catalog=load_catalog(prices),
        prices=prices,
        prompts=PromptRegistry(),
        sessionmaker=async_sessionmaker(sync_session_class=TenantSession),
        budget=StubBudget(),  # type: ignore[arg-type]
        store=store,
        retry_policy=RetryPolicy(max_retries=0, base_delay=0.0, max_delay=0.0),
        sleep=no_sleep,
    )
    return Harness(router, anthropic, openai, store)


@pytest.fixture(autouse=True)
def _no_provider_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "PAIRLY_ANTHROPIC_API_KEY",
        "PAIRLY_OPENAI_API_KEY",
        "LANGFUSE_HOST",
        "LANGFUSE_PUBLIC_KEY",
        "LANGFUSE_SECRET_KEY",
        "PAIRLY_MODEL_MODE",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def harness() -> Harness:
    return make_harness()
