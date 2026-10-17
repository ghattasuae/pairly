#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Create a local `.env` from `.env.example` with strong random secrets.

Usage:
    uv run python scripts/gen_dev_env.py                   # writes .env if missing
    uv run python scripts/gen_dev_env.py --append-missing  # also updates an existing one
    uv run python scripts/gen_dev_env.py --env PATH        # custom target (used by tests)
    uv run python scripts/gen_dev_env.py --fly DIR         # hosted demo secrets for Fly.io

Rules:
- An existing `.env` is never overwritten: no line in it is changed or removed. Without
  `--append-missing` it is left alone (the script names the keys it lacks). With it,
  every required key the file does not name at all (SECRET_GENERATORS and DERIVED, so
  keys added after the file was created) is appended with a fresh value. `make env`
  passes it when `.env` exists.
- Every secret listed in SECRET_GENERATORS gets a fresh value from `secrets`.
- Every key listed in DERIVED is computed from one of those secrets (the audit anchor
  public key is derived from the audit signing key, so the pair always matches).
- Every other key keeps the value from `.env.example` (optional keys stay empty).
- The file is created with mode 0600 and with O_EXCL, so a race cannot clobber a file.

``--fly DIR`` (the hosted demo, docs/hosted-demo.md) writes one file per Fly app into
DIR instead (``db.env``, ``api.env``, ``worker.env``, ``web.env``, ``ops.env``), each
with only the secrets that app needs, as NAME=VALUE lines for
``fly secrets import -a <app> < DIR/<file>``. Same rules: fresh values, mode 0600,
O_EXCL, an existing file is never overwritten. It prints file and key names, never a
value. The database passwords go into the URLs of the apps that use them, so the five
files always match. Values Fly creates (the Upstash Redis URL, the Tigris keys) are not
in them: the doc sets those by hand.

Standard library only.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import os
import secrets
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _hex(nbytes: int) -> Callable[[], str]:
    return lambda: secrets.token_hex(nbytes)


def _b64key() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii")


def _prefixed(prefix: str) -> Callable[[], str]:
    return lambda: f"{prefix}{secrets.token_hex(16)}"


# ---------------------------------------------------------------- Ed25519 public key
# RFC 8032 section 5.1.5 (public key from a 32-byte seed), standard library only. Used
# for the audit anchor key pair; tested against `cryptography` in scripts/tests.
_P = 2**255 - 19
_D = -121665 * pow(121666, _P - 2, _P) % _P
_SQRT_M1 = pow(2, (_P - 1) // 4, _P)


def _recover_x(y: int) -> int:
    x2 = (y * y - 1) * pow(_D * y * y + 1, _P - 2, _P) % _P
    x = pow(x2, (_P + 3) // 8, _P)
    if (x * x - x2) % _P:
        x = x * _SQRT_M1 % _P
    return _P - x if x % 2 else x


_GY = 4 * pow(5, _P - 2, _P) % _P
_GX = _recover_x(_GY)
_BASE = (_GX, _GY, 1, _GX * _GY % _P)


def _add(p: tuple[int, int, int, int], q: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    a = (p[1] - p[0]) * (q[1] - q[0]) % _P
    b = (p[1] + p[0]) * (q[1] + q[0]) % _P
    c = 2 * p[3] * q[3] * _D % _P
    d = 2 * p[2] * q[2] % _P
    e, f, g, h = b - a, d - c, d + c, b + a
    return (e * f % _P, g * h % _P, f * g % _P, e * h % _P)


def ed25519_public_key(seed: bytes) -> bytes:
    """The raw 32-byte Ed25519 public key of a 32-byte private seed."""
    if len(seed) != 32:
        raise ValueError("an Ed25519 seed is 32 bytes")
    digest = hashlib.sha512(seed).digest()
    scalar = int.from_bytes(digest[:32], "little")
    scalar &= (1 << 254) - 8
    scalar |= 1 << 254
    point, addend = (0, 1, 1, 0), _BASE
    while scalar:
        if scalar & 1:
            point = _add(point, addend)
        addend = _add(addend, addend)
        scalar >>= 1
    z_inv = pow(point[2], _P - 2, _P)
    x, y = point[0] * z_inv % _P, point[1] * z_inv % _P
    return (y | ((x & 1) << 255)).to_bytes(32, "little")


def _public_from_seed(seed_b64: str) -> str:
    seed = base64.urlsafe_b64decode(seed_b64)
    return base64.urlsafe_b64encode(ed25519_public_key(seed)).decode("ascii")


# Hex values are URL safe, so they can go straight into connection strings.
SECRET_GENERATORS: dict[str, Callable[[], str]] = {
    # First key encryption key (WP1.14). PAIRLY_KEK_2 stays empty until a rotation.
    "PAIRLY_KEK_1": _b64key,
    "PAIRLY_BLIND_INDEX_KEY": _b64key,
    # HMAC pepper for API key hashes (WP1.11).
    "PAIRLY_API_KEY_PEPPER": _b64key,
    "PAIRLY_SIGNED_LINK_SECRET": _hex(32),
    # Ed25519 seed that signs the nightly audit chain anchors (worker only, WP1.10b).
    "PAIRLY_AUDIT_SIGNING_KEY": _b64key,
    "POSTGRES_PASSWORD": _hex(24),
    "PAIRLY_OWNER_DB_PASSWORD": _hex(24),
    "PAIRLY_APP_DB_PASSWORD": _hex(24),
    "PAIRLY_AUTH_DB_PASSWORD": _hex(24),
    "REDIS_PASSWORD": _hex(24),
    "MINIO_ROOT_PASSWORD": _hex(24),
    "PAIRLY_S3_SECRET_KEY": _hex(24),
    "BETTER_AUTH_SECRET": _hex(32),
    "LANGFUSE_DB_PASSWORD": _hex(24),
    "LANGFUSE_NEXTAUTH_SECRET": _hex(32),
    "LANGFUSE_SALT": _hex(32),
    "LANGFUSE_ENCRYPTION_KEY": _hex(32),
    "CLICKHOUSE_PASSWORD": _hex(24),
    "LANGFUSE_S3_SECRET_KEY": _hex(24),
    "LANGFUSE_PUBLIC_KEY": _prefixed("pk-lf-"),
    "LANGFUSE_SECRET_KEY": _prefixed("sk-lf-"),
    "LANGFUSE_INIT_USER_PASSWORD": _hex(16),
}

# Keys computed from a generated secret: name -> (source secret, derivation).
DERIVED: dict[str, tuple[str, Callable[[str], str]]] = {
    "PAIRLY_AUDIT_ANCHOR_PUBLIC_KEY": ("PAIRLY_AUDIT_SIGNING_KEY", _public_from_seed),
}


def render(template: str) -> str:
    """Return the .env text for a .env.example template, with secrets filled in.

    Raises ValueError when a known secret is missing from the template or already
    carries a value there (the example must never hold a secret).
    """
    seen: set[str] = set()
    out: list[str] = []
    values = {key: make() for key, make in SECRET_GENERATORS.items()}
    for line in template.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            out.append(line)
            continue
        key, _, value = stripped.partition("=")
        key = key.strip()
        if key in SECRET_GENERATORS or key in DERIVED:
            if value.strip():
                raise ValueError(f".env.example must leave {key} empty")
            if key in DERIVED:
                source, derive = DERIVED[key]
                out.append(f"{key}={derive(values[source])}")
                continue
            seen.add(key)
            out.append(f"{key}={values[key]}")
        else:
            out.append(line)
    missing = sorted(set(SECRET_GENERATORS) - seen)
    if missing:
        raise ValueError(f".env.example is missing secret keys: {', '.join(missing)}")
    header = [
        "# Generated by scripts/gen_dev_env.py for LOCAL DEVELOPMENT ONLY.",
        "# Never commit this file. Delete it and rerun the script to rotate every secret",
        "# (and run `make down` with volumes removed, since Postgres keeps old passwords).",
    ]
    return "\n".join([*header, *out]) + "\n"


def write_new(path: Path, text: str) -> bool:
    """Create path with text and mode 0600. Returns False if it already exists."""
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
    return True


def env_values(text: str) -> dict[str, str]:
    """The keys a .env text assigns and their raw values (commented out lines do not count)."""
    values: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("export "):
            stripped = stripped[len("export ") :].lstrip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key, _, value = stripped.partition("=")
            values[key.strip()] = value.strip().strip("'\"")
    return values


def env_keys(text: str) -> set[str]:
    return set(env_values(text))


@dataclass
class AppendPlan:
    """What ``--append-missing`` adds to an existing .env, and what it cannot fix."""

    lines: list[tuple[str, str]] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    empty: list[str] = field(default_factory=list)

    @property
    def added(self) -> list[str]:
        return [key for key, _ in self.lines]


def plan_missing(current: str) -> AppendPlan:
    """Every required key (SECRET_GENERATORS, DERIVED) that ``current`` does not name.

    A key the file names, even with an empty value, is never touched (only reported),
    since appending a second definition would make the file ambiguous. A derived key is
    computed from its source: the generated one when both are missing, the existing value
    when only the derived key is missing. A missing source whose derived key is already
    set cannot be generated (it would not match), so it is reported instead.
    """
    existing = env_values(current)
    plan = AppendPlan()
    generated: dict[str, str] = {}
    derived_sources = {source: key for key, (source, _) in DERIVED.items()}
    for key, make in SECRET_GENERATORS.items():
        if key in existing:
            if not existing[key]:
                plan.empty.append(key)
            continue
        dependant = derived_sources.get(key)
        if dependant is not None and existing.get(dependant):
            plan.problems.append(
                f"{key} is missing but {dependant} is set; a new {key} would not match it. "
                f"Remove the {dependant} line (both are then generated) or add the matching "
                f"{key} yourself."
            )
            continue
        generated[key] = make()
        plan.lines.append((key, generated[key]))
    for key, (source, derive) in DERIVED.items():
        if key in existing:
            if not existing[key]:
                plan.empty.append(key)
            continue
        seed = generated.get(source) or existing.get(source, "")
        if not seed:
            plan.problems.append(f"{key} cannot be derived: {source} is empty or missing.")
            continue
        try:
            plan.lines.append((key, derive(seed)))
        except (ValueError, TypeError):
            plan.problems.append(f"{key} cannot be derived: {source} is not a valid key.")
    return plan


def append_missing(path: Path) -> AppendPlan:
    """Append every missing required key to ``path`` (see ``plan_missing``).

    Opens with O_APPEND (and O_NOFOLLOW), so existing bytes are never rewritten.
    """
    fd = os.open(path, os.O_RDWR | os.O_APPEND | os.O_NOFOLLOW)
    with os.fdopen(fd, "r+", encoding="utf-8") as handle:
        current = handle.read()
        plan = plan_missing(current)
        if plan.lines:
            lines = [] if current == "" or current.endswith("\n") else [""]
            lines.append(
                "# Added by scripts/gen_dev_env.py --append-missing (existing lines unchanged)."
            )
            lines.extend(f"{key}={value}" for key, value in plan.lines)
            handle.write("\n".join(lines) + "\n")
    return plan


def _report(path: Path, plan: AppendPlan) -> int:
    if plan.added:
        print(f"{path}: appended {', '.join(plan.added)} (existing lines unchanged).")
    else:
        print(f"{path}: nothing to append, leaving it unchanged.")
    for key in plan.empty:
        print(f"warning: {key} is set but empty in {path}; fill it in.", file=sys.stderr)
    for problem in plan.problems:
        print(f"error: {problem}", file=sys.stderr)
    return 1 if plan.problems else 0


def _update_existing(path: Path, *, append: bool) -> int:
    if not append:
        try:
            missing = plan_missing(path.read_text(encoding="utf-8")).added
        except OSError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        print(f"{path} exists, leaving it unchanged.")
        if missing:
            print(
                f"note: it lacks {', '.join(missing)}; run `make env` "
                "(or this script with --append-missing) to append them."
            )
        return 0
    try:
        plan = append_missing(path)
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return _report(path, plan)


# ---------------------------------------------------------------- hosted demo (--fly)
FLY_APPS = ("db", "api", "worker", "web", "ops")
FLY_DATABASE = "pairly"
# Set by hand after `fly redis create` and `fly storage create` (docs/hosted-demo.md).
FLY_FROM_FLY = ("PAIRLY_REDIS_URL", "PAIRLY_S3_ACCESS_KEY", "PAIRLY_S3_SECRET_KEY")


def fly_secrets(prefix: str = "pairly-demo") -> dict[str, dict[str, str]]:
    """Fresh secrets for each hosted demo Fly app, by app (see the module docstring)."""
    if not prefix or not all(c.islower() or c.isdigit() or c == "-" for c in prefix):
        raise ValueError("the app prefix is lower case letters, digits and dashes")
    pw = {role: secrets.token_hex(24) for role in ("postgres", "owner", "app", "auth")}
    host = f"{prefix}-db.internal:5432"

    def url(scheme: str, role: str, password: str) -> str:
        return f"{scheme}://{role}:{password}@{host}/{FLY_DATABASE}"

    signing = _b64key()
    shared = {
        "PAIRLY_DATABASE_URL": url("postgresql+asyncpg", "pairly_app", pw["app"]),
        "PAIRLY_KEK_1": _b64key(),
        "PAIRLY_BLIND_INDEX_KEY": _b64key(),
        "PAIRLY_API_KEY_PEPPER": _b64key(),
        "PAIRLY_SIGNED_LINK_SECRET": secrets.token_hex(32),
        "PAIRLY_AUDIT_ANCHOR_PUBLIC_KEY": _public_from_seed(signing),
    }
    auth_url = url("postgresql", "pairly_auth", pw["auth"])
    return {
        "db": {
            "POSTGRES_PASSWORD": pw["postgres"],
            "PAIRLY_OWNER_DB_PASSWORD": pw["owner"],
            "PAIRLY_APP_DB_PASSWORD": pw["app"],
            "PAIRLY_AUTH_DB_PASSWORD": pw["auth"],
        },
        "api": dict(shared),
        "worker": {**shared, "PAIRLY_AUDIT_SIGNING_KEY": signing},
        "web": {"BETTER_AUTH_SECRET": secrets.token_hex(32), "PAIRLY_AUTH_DATABASE_URL": auth_url},
        "ops": {
            **shared,
            "PAIRLY_DATABASE_URL_OWNER": url("postgresql+asyncpg", "pairly_owner", pw["owner"]),
            "PAIRLY_AUTH_DATABASE_URL": auth_url,
        },
    }


def write_fly(directory: Path, prefix: str = "pairly-demo") -> int:
    paths = {app: directory / f"{app}.env" for app in FLY_APPS}
    taken = [str(p) for p in paths.values() if p.exists()]
    if taken:
        print(f"error: will not overwrite {', '.join(taken)}", file=sys.stderr)
        return 1
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    for app, values in fly_secrets(prefix).items():
        text = "".join(f"{key}={value}\n" for key, value in values.items())
        if not write_new(paths[app], text):
            print(f"error: {paths[app]} appeared while writing", file=sys.stderr)
            return 1
        print(f"{paths[app]}: {', '.join(values)}")
    print("")
    print("Import each file into its app, then delete the folder:")
    for app in FLY_APPS:
        print(f"  fly secrets import --stage -a {prefix}-{app} < {paths[app]}")
    print(f"Set by hand on api, worker and ops (values from Fly): {', '.join(FLY_FROM_FLY)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--example", type=Path, default=ROOT / ".env.example")
    parser.add_argument("--env", type=Path, default=ROOT / ".env")
    parser.add_argument(
        "--append-missing",
        action="store_true",
        help="for an existing .env: append every required key it lacks, change nothing else",
    )
    parser.add_argument(
        "--fly",
        type=Path,
        metavar="DIR",
        help="hosted demo: write one secrets file per Fly app into DIR (no .env)",
    )
    parser.add_argument("--fly-prefix", default="pairly-demo", help="Fly app name prefix")
    args = parser.parse_args(argv)

    if args.fly is not None:
        try:
            return write_fly(args.fly, args.fly_prefix)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
    if args.env.exists():
        return _update_existing(args.env, append=args.append_missing)
    try:
        text = render(args.example.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if not write_new(args.env, text):
        return _update_existing(args.env, append=args.append_missing)
    print(f"created {args.env} with {len(SECRET_GENERATORS)} generated secrets (mode 0600).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
