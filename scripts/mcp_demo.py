#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""The MCP part of the engineer demo (BRIEF 10.3), without Claude. Development only.

    PAIRLY_API_KEY=pk_test_... uv run --package pairly-mcp python -m scripts.mcp_demo

Starts the Pairly MCP server on stdio exactly as Claude Desktop or Claude Code would
(``uv run --package pairly-mcp pairly-mcp``), lists its tools and makes the call Claude
makes for "Which approvals are waiting for me?" (``list_my_approvals``), then asks it to
approve the first one the only way it can: ``propose_action``. It prints a transcript.
Everything is printed as it comes back; the API key is never printed.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

QUESTION = "Which approvals are waiting for me?"


def _text(result: Any) -> Any:
    for part in result.content:
        if getattr(part, "type", "") == "text":
            try:
                return json.loads(part.text)
            except json.JSONDecodeError:
                return part.text
    return None


def _show(value: Any) -> str:
    return json.dumps(value, indent=2, ensure_ascii=False)


async def run() -> int:
    if not os.environ.get("PAIRLY_API_KEY"):
        print("usage: PAIRLY_API_KEY=pk_test_... python -m scripts.mcp_demo", file=sys.stderr)
        return 2
    server = StdioServerParameters(
        command="uv",
        args=["run", "--quiet", "--package", "pairly-mcp", "pairly-mcp"],
        env={
            k: v for k, v in os.environ.items() if k.startswith(("PAIRLY_", "PATH", "HOME", "UV_"))
        },
    )
    async with stdio_client(server) as (read, write), ClientSession(read, write) as session:
        info = await session.initialize()
        print(f"$ pairly-mcp (stdio), server {info.server_info.name} {info.server_info.version}")
        tools = await session.list_tools()
        print(f"tools: {', '.join(t.name for t in tools.tools)}")
        print()
        print(f'> "{QUESTION}"')
        print("tool call: list_my_approvals {}")
        approvals = _text(await session.call_tool("list_my_approvals", {}))
        print(_show(approvals))
        items = approvals.get("items", []) if isinstance(approvals, dict) else []
        if items:
            first = items[0]
            print()
            print(f'> "Approve {first.get("title")}."')
            arguments = {
                "entity_type": "approval",
                "entity_id": first["id"],
                "action": "approve",
                "body": "Suggest approving: the evidence matches the request.",
            }
            print(f"tool call: propose_action {_show(arguments)}")
            proposed = await session.call_tool("propose_action", arguments)
            print(_show(_text(proposed)))
    return 0


def main() -> int:
    return asyncio.run(run())


if __name__ == "__main__":
    raise SystemExit(main())
