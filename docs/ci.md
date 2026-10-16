# Continuous integration

CI runs on GitHub Actions for every pull request and every push to `main`. The workflow is `.github/workflows/ci.yml`. Every check also runs on your machine with `make`.

## Jobs

| Job | What it checks | Run it locally |
|---|---|---|
| `python` | ruff lint and format, import contracts, mypy (strict on `packages/core`), Alembic migrations, pytest with coverage against a real Postgres | `make lint typecheck ci-test` |
| `js` | pnpm install from the lockfile, eslint, typecheck, vitest, Next.js build of `apps/web` | `pnpm install --frozen-lockfile && pnpm lint && pnpm typecheck && pnpm test` |
| `text` | no em dashes in docs and UI text, SPDX header in every source file, workflow lint (actionlint) | `make lint ci-actionlint` |
| `security` | Bandit, pip-audit, pnpm audit, gitleaks over the full git history | `make ci-security` |
| `licenses` | every dependency has a license compatible with AGPL-3.0 and commercial relicensing | `make ci-licenses` |
| `evals` | placeholder that runs `make evals`; becomes the eval merge gate in WP2.10 | `make evals` |
| `docker` | builds `infra/docker/python.Dockerfile` (no push) | `docker build -f infra/docker/python.Dockerfile .` |
| `ci-ok` | passes only when every job above passed | |

### Python tests and the database

The `python` job starts a throwaway Postgres with `scripts/ci_postgres.sh`. It uses the same image as `infra/docker-compose.yml` and runs the same init SQL from `infra/postgres/init`, so the `pairly_owner`, `pairly_app` and `pairly_auth` roles match the dev stack. All passwords are random and masked in the logs.

Alembic runs `upgrade heads` (core plus every workflow branch) as `pairly_owner` first. Then pytest runs. Database tests connect as `pairly_app`, so Row Level Security applies to them. If the database tests are skipped for any reason, the job fails: CI never passes with the database tests silently turned off.

The coverage threshold comes from `[tool.coverage.report] fail_under` in `pyproject.toml`. Change it there, not in the workflow.

`make ci-test` does the same on your machine with Docker (port 55432, `CI_PG_PORT` to change it; stop the native cluster first, it uses the same port). Without Docker, `make local-test` runs the same steps against fresh test databases in the native Postgres cluster (`scripts/local_pg.sh`).

### Web build

`apps/web` becomes a Next.js app in WP0.6. Until it has a `build` script, a `next` dependency and an `app` directory, the `js` job skips the build step and prints a notice. No change is needed when WP0.6 lands.

## Security scans and thresholds

1. **Bandit** (`scripts/bandit.yaml`): fails on findings of medium or higher severity with medium or higher confidence. Tests are excluded. Ruff also runs the same rule set (`S`) on every file. To accept one reviewed finding, add `# nosec BXXX` with a reason on that line.
2. **pip-audit**: audits the exact locked Python versions (`uv export`, with hashes). Any known vulnerability fails the job.
3. **pnpm audit**: production dependencies fail on `high` or `critical`. Dev-only dependencies (build and test tools that never ship) fail only on `critical`. Lower findings appear in the log. Raise the bar here, never lower it without a written reason in the pull request.
4. **gitleaks**: scans every commit. Settings and the few proven false positives are in `.gitleaks.toml`. If a real secret is found, rotate it first, then remove it from history.

Tool versions are pinned. gitleaks and actionlint binaries are downloaded by `scripts/ci_tools.sh`, which checks a pinned SHA-256 before use.

## License gate

`scripts/check_licenses.py` reads the installed Python packages (the uv environment) and the pnpm dependency tree (`pnpm licenses list --json -r`) and sorts each license into one of four groups.

| Group | Licenses | Result |
|---|---|---|
| allow | MIT, MIT-0, BSD-2-Clause, BSD-3-Clause, Apache-2.0, ISC, MPL-2.0, PSF-2.0, Python-2.0, 0BSD, Unlicense, CC0-1.0, BlueOak-1.0.0, Zlib | pass |
| review | LGPL, EPL | pass with a warning; check that the library is used as a separate, replaceable library |
| deny | GPL, AGPL, SSPL, BUSL, Commons Clause | fail |
| unknown | anything else, or no license | fail |

SPDX expressions work as expected: `MIT OR GPL-3.0` passes, `MIT AND GPL-3.0` fails.

A denied or unknown package can pass only with an entry in `scripts/license-exceptions.yaml` that has a written reason. The entry matches the exact license string, so if a package changes its license, it needs a new review. Our own packages (`pairly*` and `@pairly/*`) are skipped.

Container images that run as separate processes are not dependencies and are not checked here. Examples: ClamAV (GPL) and MinIO (AGPL). The exceptions file explains why.

## Dependency updates

Dependabot (`.github/dependabot.yml`) opens grouped pull requests every Monday for uv, pnpm, GitHub Actions, Dockerfile base images and compose images. Security updates arrive as soon as an advisory is published. Each update goes through the full CI, including the license gate.

GitHub Actions are pinned to full commit SHAs with the version in a comment. Dependabot keeps both up to date.

Create the labels Dependabot uses before the first run: `dependencies`, `python`, `javascript`, `ci`, `docker`.

## Branch protection for `main`

Set these when the repository is on GitHub (Settings, Rules, Rulesets, or Branches, Branch protection rules).

1. Require a pull request before merging.
   1. Required approvals: 1.
   2. Dismiss stale approvals when new commits are pushed.
   3. Require review from code owners, once a `CODEOWNERS` file exists.
   4. Require approval of the most recent push.
2. Require status checks to pass before merging.
   1. Require branches to be up to date before merging.
   2. Required checks:
      1. `ci-ok` (from the CI workflow). It already depends on every CI job, so new jobs need no settings change.
      2. `cla` (from the CLA Assistant workflow), so unsigned contributions cannot merge.
3. Require conversation resolution before merging.
4. Require linear history.
5. Block force pushes and deletions.
6. Do not allow bypassing the rules above, including for administrators.

Repository settings:

1. Actions, General, Workflow permissions: "Read repository contents and packages permissions". Each workflow asks for more only where needed (only the CLA workflow does).
2. Actions, General: require approval for workflows from first-time contributors and from outside collaborators.
3. Actions, General, allowed actions: allow GitHub actions plus the actions listed in the workflows (`astral-sh/setup-uv`, `pnpm/action-setup`, `docker/*`, `contributor-assistant/github-action`).
4. Code security: turn on Dependabot alerts, Dependabot security updates, secret scanning and push protection.

## Running a job with act

[act](https://github.com/nektos/act) can run a job in Docker before the repository is on GitHub, for example `act pull_request -j python`. It is optional and heavy on CPU. The `make` targets above cover the same checks with less overhead.
