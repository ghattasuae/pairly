# Pairly AI

AI does the work. You make the call. Together.

Pairly is open source (AGPL-3.0) with a commercial license option. It has one workflow-agnostic core engine and two workflows on top: Pairly Hire and Pairly Pay.

**Status: work in progress.** Nothing here is ready to use yet.

## Requirements

- [uv](https://docs.astral.sh/uv/) (installs and manages Python 3.12 for you)
- Node.js 22 with Corepack enabled (`corepack enable`), which provides pnpm
- For the local stack: Postgres 16 with pgvector, Valkey and Tesseract, installed natively
  (on macOS: Homebrew `postgresql@16`, `pgvector`, `valkey`, `tesseract`). No Docker.

## Getting started

```bash
make install      # Python and JS dependencies
make test         # Python and JS tests
make dev          # start the native stack: web http://localhost:3100, API http://localhost:8000
make demo         # the native stack plus a complete Hire and expense demo, offline
make local-status # what runs, with health checks
make local-test   # pytest against fresh local test databases
make local-down   # stop everything
```

`make dev` creates `.env` with random secrets on first run, then starts Postgres and
Valkey as local processes (data in `.local/`), runs the migrations and starts the API,
the worker and the web app in the background at low priority. Email goes to files in
`.local/mail` and documents to `.local/objects`. See `infra/README.md` for the details.

`make demo` starts the stack like `make dev` and seeds the organisation `demo`, offline
in recorded model mode (no API key): people for every role (admin, recruiter, hiring
manager, interviewer, an employee who refers, and the expense roles) with their
invitations accepted, two open backend engineer jobs with locked rubrics, ten synthetic
CVs screened from the committed recordings, candidates in every pipeline stage, a CV
with hidden white text that the guardrails flag, two booked interviews on the fake
calendar, a candidate waiting past the response SLA, and the expense example. It prints
the sign-in addresses: sign in with a magic link, which lands in `.local/mail`. Running
it again changes nothing. The stack keeps running until `make local-down`.

To try the expense example in your own organisation: `make bootstrap-org NAME="Acme Ltd" SLUG=acme
ADMIN_EMAIL=you@acme.test`, open the printed invite link, then `make demo-expense
SLUG=acme`.

The Docker Compose stack (`make docker-dev`) is for CI and self-hosters. Run `make help`
to see every target. `make evals` is not implemented yet.

## Repository layout

| Path | What it holds |
|---|---|
| `apps/api` | REST API |
| `apps/worker` | Background jobs and scheduled tasks |
| `apps/web` | Web app |
| `apps/mcp` | MCP server (read and propose tools only) |
| `packages/core` | Core engine (`pairly_core`) |
| `packages/hire` | Pairly Hire workflow (`pairly_hire`) |
| `packages/pay` | Pairly Pay workflow (`pairly_pay`) |
| `packages/shared-ts` | TypeScript types generated from the API schema |
| `sdks/` | Python and TypeScript clients |
| `evals/` | Evaluation datasets, configs and runners |
| `synth/` | Synthetic data generators |
| `infra/` | Docker Compose, migrations and seed scripts |
| `docs/` | Architecture, ADRs and guides |

## License

AGPL-3.0-only, with a commercial license option from Proofly Technology. See `LICENSING.md`.
