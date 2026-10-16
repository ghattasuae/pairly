# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""The Pairly MCP server: read and propose tools over stdio. People decide.

Tools (every one calls the API through ``pairly_mcp.client``, so the API's RBAC, Row
Level Security, blind screening and API key limits apply):

* ``search_candidates``      candidates, blind-safe (names only where the API shows them)
* ``get_candidate_summary``  one application, without contact details or files
* ``list_my_approvals``      what is waiting for you (the key's creator, or the JWT's person)
* ``get_approval``           one approval as the API shows it to you (masked while blind)
* ``get_invoice_status``     where an invoice stands, by id, reference or number
* ``list_exceptions``        Pay invoices waiting for a person, by category
* ``propose_action``         a note or a suggested action for a person to decide
* ``get_decision_receipt``   the receipt of one decision record

No tool approves, decides, pays, exports or deletes. ``propose_action`` only records a
proposal (``POST /v1/proposals``); a person acts on it in Pairly.
"""

from __future__ import annotations

from typing import Annotated, Any, Final, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations
from pydantic import Field

from pairly_mcp import __version__
from pairly_mcp.client import ApiError, PairlyApi, decision_segment, uuid_segment
from pairly_mcp.config import load_config

HUMANS_DECIDE: Final = (
    "Humans decide: this tool only reads (or proposes). A person decides every outcome in "
    "Pairly; nothing here approves, rejects, pays or deletes anything."
)

INSTRUCTIONS: Final = (
    "Pairly MCP server. You can read candidates (blind-safe), approvals, invoices, Pay "
    "exceptions and decision receipts, and you can propose an action or leave a note. "
    "You cannot decide: people approve, reject, pay and hire in Pairly. When the user "
    "wants something decided, use propose_action and give them the link to decide."
)

_READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=False)
_PROPOSE = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False
)

Limit = Annotated[int, Field(ge=1, le=50)]


def _pick(data: Any, *keys: str) -> dict[str, Any]:
    if not isinstance(data, dict):
        return {}
    return {k: data.get(k) for k in keys if k in data}


class PairlyTools:
    """The tool implementations, bound to one API client (testable without stdio)."""

    def __init__(self, api: PairlyApi) -> None:
        self.api = api

    def _link(self, path: str) -> str | None:
        web = self.api.config.web_url
        return f"{web}/en/{path}" if web else None

    async def _call(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            return await self.api.request(method, path, **kwargs)
        except ApiError as exc:
            hint = ""
            if exc.status in {401, 403}:
                hint = " (check the API key's scopes or the token's organisation)"
            raise ToolError(f"Pairly said {exc.status} {exc.title}: {exc.detail}{hint}") from None

    # ------------------------------------------------------------------ Hire

    async def search_candidates(
        self,
        query: str | None = None,
        job_id: str | None = None,
        needs_review: bool | None = None,
        limit: int = 10,
    ) -> dict[str, Any]:
        if query is not None and query.strip():
            hits = await self._call(
                "POST",
                "/v1/hire/rediscovery/search",
                json={"query": query.strip()[:500], "limit": limit},
            )
            items = [
                _pick(h, "candidate_id", "label", "identity_hidden", "applications", "score")
                for h in hits or []
            ]
            return {"source": "talent_pool", "items": items, "note": HUMANS_DECIDE}
        params: dict[str, Any] = {"limit": limit, "needs_review": needs_review}
        if job_id is not None:
            params["job_id"] = _arg(uuid_segment, job_id, "job_id")
        data = await self._call("GET", "/v1/hire/applications", params=params)
        items = [
            _pick(
                a,
                "id",
                "job_id",
                "reference",
                "status",
                "candidate_name",
                "identity_hidden",
                "needs_human_review",
                "review_reasons",
                "submitted_at",
            )
            for a in (data or {}).get("items", [])
        ]
        return {"source": "applications", "items": items, "note": HUMANS_DECIDE}

    async def get_candidate_summary(self, application_id: str) -> dict[str, Any]:
        segment = _arg(uuid_segment, application_id, "application_id")
        data = await self._call("GET", f"/v1/hire/pipeline/applications/{segment}")
        summary = _pick(
            data,
            "application_id",
            "job_id",
            "job_title",
            "reference",
            "status",
            "stage_key",
            "stage_label",
            "submitted_at",
            "blind_mode",
            "identity_hidden",
            "label",
            "tags",
            "needs_human_review",
            "review_reasons",
            "talent_pool",
        )
        # Contact details, files, the referrer, messages and the timeline are never
        # returned: a summary is about the evidence, not the person's identity.
        screening = data.get("screening") if isinstance(data, dict) else None
        if isinstance(screening, dict):
            summary["screening"] = _pick(screening, "status", "overall", "criteria")
        following = data.get("next_stage") if isinstance(data, dict) else None
        summary["next_stage"] = _pick(following, "key", "label") or None
        summary["note"] = HUMANS_DECIDE
        return summary

    # ------------------------------------------------------------------ approvals

    async def list_my_approvals(
        self,
        workflow: str | None = None,
        status: Literal["pending", "approved", "rejected", "changes_requested"] = "pending",
        limit: int = 20,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"assigned_to_me": "true", "status": status, "limit": limit}
        if workflow is not None:
            params["workflow"] = workflow
        data = await self._call("GET", "/v1/approvals", params=params)
        items = []
        for a in (data or {}).get("items", []):
            item = _pick(
                a,
                "id",
                "workflow",
                "approval_type",
                "title",
                "status",
                "sla_state",
                "created_at",
                "guardrail_findings",
            )
            step = a.get("current_step") if isinstance(a, dict) else None
            item["step"] = _pick(step, "name", "due_at") or None
            item["decide_in_pairly"] = self._link(f"inbox/{a.get('id')}")
            items.append(item)
        return {
            "items": items,
            "more": bool((data or {}).get("next_cursor")),
            "note": HUMANS_DECIDE,
        }

    async def get_approval(self, approval_id: str) -> dict[str, Any]:
        segment = _arg(uuid_segment, approval_id, "approval_id")
        data = await self._call("GET", f"/v1/approvals/{segment}")
        out = _pick(
            data,
            "id",
            "workflow",
            "approval_type",
            "title",
            "status",
            "sla_state",
            "entity_type",
            "entity_id",
            "context",
            "payload",
            "masked_fields",
            "display_note",
            "policy_version",
            "chain_name",
            "created_at",
            "decided_at",
        )
        out["steps"] = [
            _pick(s, "position", "name", "mode", "status", "due_at", "completed_at")
            for s in (data or {}).get("steps", [])
        ]
        out["history"] = [
            _pick(h, "decision", "reason", "channel", "created_at")
            for h in (data or {}).get("history", [])
        ]
        out["decide_in_pairly"] = self._link(f"inbox/{segment}")
        out["note"] = HUMANS_DECIDE
        return out

    # ------------------------------------------------------------------ Pay

    async def get_invoice_status(self, invoice: str) -> dict[str, Any]:
        text = invoice.strip()
        try:
            segment = uuid_segment(text, "invoice")
        except ValueError:
            if not text or len(text) > 100:
                raise ToolError("give an invoice id, reference or invoice number") from None
            found = await self._call("GET", "/v1/pay/invoices", params={"q": text, "limit": 10})
            rows = (found or {}).get("items", [])
            exact = [
                r
                for r in rows
                if text.lower()
                in {str(r.get("reference", "")).lower(), str(r.get("invoice_number", "")).lower()}
            ]
            matches = exact or rows
            if len(matches) != 1:
                return {
                    "matches": [
                        _pick(r, "id", "reference", "invoice_number", "vendor_name", "status")
                        for r in matches
                    ],
                    "note": "Several or no invoices match; ask with one of these ids.",
                }
            segment = uuid_segment(str(matches[0].get("id")), "invoice")
        data = await self._call("GET", f"/v1/pay/invoices/{segment}")
        out = _pick(
            data,
            "id",
            "reference",
            "status",
            "vendor_name",
            "invoice_number",
            "invoice_date",
            "due_date",
            "currency",
            "total",
            "review_reasons",
            "checks_state",
            "blockers",
            "payment_run_id",
            "paid_at",
            "decision_ids",
        )
        approval = data.get("approval") if isinstance(data, dict) else None
        out["approval"] = (
            _pick(approval, "approval_id", "submitted_at", "approved_at", "approved_by_rule")
            or None
        )
        out["open_in_pairly"] = self._link(f"pay/invoices/{segment}")
        out["note"] = HUMANS_DECIDE
        return out

    async def list_exceptions(
        self, category: str | None = None, query: str | None = None, limit: int = 20
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"category": category, "limit": limit}
        if query is not None and query.strip():
            params["q"] = query.strip()[:100]
        data = await self._call("GET", "/v1/pay/exceptions", params=params)
        items = []
        for row in (data or {}).get("items", []):
            item = _pick(
                row,
                "id",
                "reference",
                "vendor_name",
                "invoice_number",
                "po_number",
                "currency",
                "total",
                "age_hours",
                "overdue",
            )
            item["categories"] = [
                {
                    "category": c.get("category"),
                    "why": [e.get("en") for e in c.get("explanations", []) if isinstance(e, dict)],
                    "how_to_resolve": (c.get("resolution") or {}).get("en"),
                }
                for c in row.get("categories", [])
                if isinstance(c, dict)
            ]
            item["open_in_pairly"] = self._link(f"pay/invoices/{row.get('id')}")
            items.append(item)
        return {
            **_pick(data, "counts", "total", "overdue", "sla_hours"),
            "items": items,
            "note": HUMANS_DECIDE,
        }

    # ------------------------------------------------------------------ propose, receipts

    async def propose_action(
        self,
        entity_type: str,
        entity_id: str,
        body: str,
        action: str | None = None,
        workflow: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "entity_type": entity_type,
            "entity_id": entity_id,
            "kind": "action" if action else "note",
            "body": body,
        }
        if action:
            payload["action"] = action
        if workflow:
            payload["workflow"] = workflow
        data = await self._call("POST", "/v1/proposals", json=payload)
        out = _pick(
            data, "id", "kind", "action", "entity_type", "entity_id", "workflow", "created_at"
        )
        out["status"] = "proposed: waiting for a person to decide"
        if entity_type == "approval":
            out["decide_in_pairly"] = self._link(f"inbox/{entity_id}")
        out["note"] = HUMANS_DECIDE
        return out

    async def get_decision_receipt(self, decision_id: str) -> dict[str, Any]:
        segment = _arg(decision_segment, decision_id)
        data = await self._call("GET", f"/v1/decisions/{segment}/receipt")
        return {"receipt": data, "note": HUMANS_DECIDE}


def _arg(check: Any, value: str, *what: str) -> str:
    try:
        result: str = check(value, *what)
    except ValueError as exc:
        raise ToolError(str(exc)) from None
    return result


def build_server(api: PairlyApi) -> MCPServer[Any]:
    """The MCP server with every tool registered against ``api``."""
    tools = PairlyTools(api)
    server: MCPServer[Any] = MCPServer(
        name="pairly", version=__version__, instructions=INSTRUCTIONS
    )

    @server.tool(
        description=(
            "Search candidates. With a query: the talent pool (people who agreed to be "
            "found again), by skills or experience. Without one: recent applications, "
            "optionally for one job or only those needing a person's review. Blind-safe: "
            "while screening is blind you get 'Candidate <reference>', never a name or "
            "contact details. " + HUMANS_DECIDE
        ),
        annotations=_READ_ONLY,
    )
    async def search_candidates(
        query: str | None = None,
        job_id: str | None = None,
        needs_review: bool | None = None,
        limit: Limit = 10,
    ) -> dict[str, Any]:
        return await tools.search_candidates(query, job_id, needs_review, limit)

    @server.tool(
        description=(
            "Summarise one application: stage, screening criteria with evidence quotes, "
            "and why it needs a person's review. Blind-safe: no name while blind, never "
            "contact details or files. " + HUMANS_DECIDE
        ),
        annotations=_READ_ONLY,
    )
    async def get_candidate_summary(application_id: str) -> dict[str, Any]:
        return await tools.get_candidate_summary(application_id)

    @server.tool(
        description=(
            "List the approvals waiting for me (the person this server signs in as, or "
            "the creator of its API key), with due dates and a link to decide in Pairly. "
            "Use it for 'which approvals are waiting for me?'. " + HUMANS_DECIDE
        ),
        annotations=_READ_ONLY,
    )
    async def list_my_approvals(
        workflow: str | None = None,
        status: Literal["pending", "approved", "rejected", "changes_requested"] = "pending",
        limit: Limit = 20,
    ) -> dict[str, Any]:
        return await tools.list_my_approvals(workflow, status, limit)

    @server.tool(
        description=(
            "Show one approval: what is asked, the evidence, the approval chain and its "
            "history. Fields hidden from you (for example while blind) stay hidden. "
            + HUMANS_DECIDE
        ),
        annotations=_READ_ONLY,
    )
    async def get_approval(approval_id: str) -> dict[str, Any]:
        return await tools.get_approval(approval_id)

    @server.tool(
        description=(
            "Where an invoice stands (Pairly Pay): status, checks, what blocks it, its "
            "approval and payment. Accepts the invoice id, the Pairly reference or the "
            "vendor's invoice number. " + HUMANS_DECIDE
        ),
        annotations=_READ_ONLY,
    )
    async def get_invoice_status(invoice: str) -> dict[str, Any]:
        return await tools.get_invoice_status(invoice)

    @server.tool(
        description=(
            "List Pay exceptions: invoices waiting for a person, with each problem "
            "(no PO, price variance, duplicate suspect, bank mismatch and so on), why, and "
            "how to resolve it. " + HUMANS_DECIDE
        ),
        annotations=_READ_ONLY,
    )
    async def list_exceptions(
        category: str | None = None, query: str | None = None, limit: Limit = 20
    ) -> dict[str, Any]:
        return await tools.list_exceptions(category, query, limit)

    @server.tool(
        description=(
            "Propose an action or leave a note for a person to decide. entity_type is "
            "for example 'approval', 'pay.invoice' or 'hire.application'; action (optional) "
            "is what you suggest, for example 'approve', 'reject', 'hold' or "
            "'request_changes'. This only records the proposal: it never approves, "
            "decides, pays or deletes. A person reviews it in Pairly and decides."
        ),
        annotations=_PROPOSE,
    )
    async def propose_action(
        entity_type: str,
        entity_id: str,
        body: Annotated[str, Field(min_length=1, max_length=4000)],
        action: str | None = None,
        workflow: str | None = None,
    ) -> dict[str, Any]:
        return await tools.propose_action(entity_type, entity_id, body, action, workflow)

    @server.tool(
        description=(
            "Get the receipt of one decision record (dec_...): inputs, policy and prompt "
            "versions, the AI output with evidence, and the person who made the final "
            "call. " + HUMANS_DECIDE
        ),
        annotations=_READ_ONLY,
    )
    async def get_decision_receipt(decision_id: str) -> dict[str, Any]:
        return await tools.get_decision_receipt(decision_id)

    return server


async def _serve() -> None:
    api = PairlyApi(load_config())
    try:
        await build_server(api).run_stdio_async()
    finally:
        await api.aclose()


def main() -> None:
    """``pairly-mcp``: run the server on stdio (Claude Desktop, Claude Code)."""
    import anyio

    anyio.run(_serve)


__all__ = ["HUMANS_DECIDE", "PairlyTools", "build_server", "main"]
