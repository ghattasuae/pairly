# pairly-mcp

The Pairly MCP server. It lets Claude (Desktop or Code) read what is waiting in Pairly
and propose actions. **People decide**: no tool approves, rejects, pays, exports or
deletes anything.

| Tool | What it does |
|---|---|
| `search_candidates` | Talent pool search by skills, or recent applications. Blind-safe. |
| `get_candidate_summary` | One application: stage, screening criteria and evidence. No contact details or files. |
| `list_my_approvals` | The approvals waiting for you, with due dates and a link to decide in Pairly. |
| `get_approval` | One approval as Pairly shows it to you (fields masked while blind stay masked). |
| `get_invoice_status` | Where an invoice stands, by id, Pairly reference or invoice number. |
| `list_exceptions` | Pay invoices waiting for a person, with each problem and how to resolve it. |
| `propose_action` | Records a note or a suggested action (`POST /v1/proposals`) for a person. |
| `get_decision_receipt` | The receipt of one decision record (`dec_...`). |

## How it stays safe

* Every tool calls the Pairly API over HTTP. Authentication, Row Level Security, RBAC,
  blind screening and the API key limits apply on the server: an API key can only
  read, propose and upload, never approve or decide.
* The client has a fixed route allowlist (`pairly_mcp/client.py`): reads, two searches
  and `POST /v1/proposals`. Ids are validated before a URL is built.
  `tests/test_mcp_routes.py` calls every tool with hostile arguments and checks that
  no request reaches an approve, decide or pay route;
  `apps/api/tests/test_mcp_server.py` does the same against the real API.

## Credentials

Set exactly one of:

* `PAIRLY_API_KEY`: an API key with these scopes: `core.approval.read` (your inbox),
  `core.proposal.propose`, plus the reads you want, for example `pay.invoice.read`,
  `hire.candidate.read`, `core.decision.read`. A key reads the inbox of the person who
  created it, read only. Sandbox organisations use `pk_test_` keys
  (`pairly-admin sandbox create` prints one).
* `PAIRLY_TOKEN` and `PAIRLY_ORG`: a person's short-lived JWT and the organisation id.

Optional: `PAIRLY_API_URL` (default `http://127.0.0.1:8000`, the native stack; plain
http only for this machine) and `PAIRLY_WEB_URL` (for "decide in Pairly" links, for
example `http://localhost:3100`).

## Run it

```sh
make dev                                   # the native stack
make sandbox NAME="Demo"                   # sandbox org with 2 pending approvals; prints a pk_test_ key
PAIRLY_API_KEY=pk_test_... make mcp        # stdio server (Ctrl-C to stop)
```

### Claude Desktop

`claude_desktop_config.json` (Settings, Developer, Edit config):

```json
{
  "mcpServers": {
    "pairly": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/Pairly", "--package", "pairly-mcp", "pairly-mcp"],
      "env": {
        "PAIRLY_API_URL": "http://127.0.0.1:8000",
        "PAIRLY_WEB_URL": "http://localhost:3100",
        "PAIRLY_API_KEY": "pk_test_..."
      }
    }
  }
}
```

### Claude Code

```sh
claude mcp add pairly \
  --env PAIRLY_API_URL=http://127.0.0.1:8000 \
  --env PAIRLY_WEB_URL=http://localhost:3100 \
  --env PAIRLY_API_KEY=pk_test_... \
  -- uv run --directory /path/to/Pairly --package pairly-mcp pairly-mcp
```

Then ask: **"Which approvals are waiting for me?"** Claude calls `list_my_approvals`
and answers with each approval, its due date and the link to decide it in Pairly. Ask it
to approve one and it proposes instead (`propose_action`), telling you to decide in
Pairly.
