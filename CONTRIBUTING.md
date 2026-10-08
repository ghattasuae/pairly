# Contributing to Pairly AI

Thank you for helping. This guide explains how to set up the project, how changes get merged, and why we ask you to sign a CLA.

By taking part, you agree to follow our [Code of Conduct](CODE_OF_CONDUCT.md). To report a security problem, do not open an issue. Follow [SECURITY.md](SECURITY.md).

## Set up

You need:

1. [uv](https://docs.astral.sh/uv/) for Python (Python 3.12).
2. [pnpm](https://pnpm.io/) for JavaScript and TypeScript.
3. Postgres 16 with pgvector, Valkey and Tesseract, installed natively, for the local
   stack (on macOS: Homebrew `postgresql@16`, `pgvector`, `valkey`, `tesseract`). Docker
   is not needed for development.
4. `make`.

Then run:

```sh
make install   # Python and JS dependencies
make hooks     # pre-commit hooks (lint, types, secrets scan, SPDX headers, em dash check)
make test      # Python and JS tests
make dev       # native stack: Postgres, Valkey, api, worker, web on http://localhost:3100
make local-test  # pytest against fresh local test databases, no Docker
make local-down  # stop every native process
```

Run `make help` to see every target. The native stack keeps all its data under `.local/`
(gitignored); see `infra/README.md`. The Docker Compose stack (`make docker-dev`) is for
CI and self-hosters.

## Branches and pull requests

1. Open an issue first for anything larger than a small fix, so we can agree on the approach.
2. Create a branch from `main`. Use a short name such as `fix/duplicate-invoice-check` or `feat/interview-kit`.
3. Keep each pull request focused on one change.
4. Add or update tests. If you change AI behaviour, run `make evals` and include the results.
5. Run `make lint` and `make test` before you push.
6. Open the pull request and fill in the checklist in the template.
7. Sign the CLA when the bot asks. We cannot merge until it is signed.
8. A maintainer reviews the change. CI must pass, including the eval thresholds.

## Rules for every change

### Humans decide

Agents propose, humans approve. No code may let an agent reject a candidate, send an offer, approve a payment, change vendor bank details or delete data on its own. This is enforced in code, not only in prompts.

### Synthetic data only

Never commit real personal data. That means no real CVs, invoices, names, email addresses, phone numbers or bank details. All fixtures, test data and demo data must be generated. If you find real data in the repository, report it privately using [SECURITY.md](SECURITY.md).

### License headers

Every source file starts with this header, in the comment syntax of the file type:

```
SPDX-License-Identifier: AGPL-3.0-only
Copyright (c) 2026 Proofly Technology
```

Check and fix headers with:

```sh
uv run python scripts/spdx_headers.py --check
uv run python scripts/spdx_headers.py --fix
```

### Dependencies

New dependencies must use a license that is compatible with AGPL-3.0 and with commercial relicensing (for example MIT, BSD, Apache-2.0, ISC, MPL-2.0). Do not add AGPL or GPL libraries that would be linked into Pairly. CI checks this. After adding a dependency, regenerate `NOTICE.md` with `scripts/third_party_notices.py`.

### Copy style

This applies to UI text, emails, docs, comments meant for users, and the README:

1. Plain, short sentences.
2. No hype words.
3. Never use em dashes. Use commas, colons or full stops instead.
4. Do not invent numbers. Any metric in docs must come from a real eval run.

## Contributor License Agreement (CLA)

### Why we ask for it

Pairly is open source (AGPL-3.0) with a commercial license option. Proofly Technology offers the same code under both licenses. That only works if Proofly Technology has the right to relicense every part of the code, including your contribution.

Without a CLA, your contribution would be available under AGPL-3.0 only. Proofly Technology could not include it in the commercial license, and the dual licensing model would break.

### What the CLA does

1. **You keep your copyright.** You do not transfer ownership of your contribution.
2. You grant Proofly Technology a broad license to use your contribution, including the right to sublicense it and to relicense it under other terms, such as the commercial license.
3. You confirm that you have the right to contribute the code.

The full text is in [CLA.md](CLA.md). The CLA bot asks you to sign it on your first pull request. You sign once, by posting a comment on the pull request.

### Our pledge

Pairly AI will remain available under AGPL-3.0. Proofly Technology will not withdraw the open source license from code that has been released under it, and it will keep releasing Pairly under AGPL-3.0. The commercial license is an additional option for people who want different terms. It does not replace the open source version.

We know a CLA asks for trust. If you have questions or concerns, open a discussion before you contribute.

## Questions

Open a GitHub issue or discussion. For licensing questions, read [LICENSING.md](LICENSING.md) first.
