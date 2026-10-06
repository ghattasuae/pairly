# Pairly AI

AI does the work. You make the call. Together.

Pairly is open source (AGPL-3.0) with a commercial license option. It has one workflow-agnostic core engine and two workflows on top: Pairly Hire and Pairly Pay.

**Status: work in progress.** Nothing here is ready to use yet.

## Requirements

- [uv](https://docs.astral.sh/uv/) (installs and manages Python 3.12 for you)
- Node.js 22 with Corepack enabled (`corepack enable`), which provides pnpm

## Getting started

```bash
make install   # Python and JS dependencies
make test      # Python and JS tests
```

Run `make help` to see every target. `make dev`, `make demo` and `make evals` are not implemented yet.

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
